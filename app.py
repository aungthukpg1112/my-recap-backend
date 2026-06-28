import os
import uuid
import subprocess
import threading
import json
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = '/tmp/recap_jobs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
jobs = {}

NORMAL_DUR = 4.0
FREEZE_DUR = 2.0

def get_duration(path):
    r = subprocess.run(
        ['ffprobe','-v','quiet','-print_format','json','-show_format',path],
        capture_output=True, text=True
    )
    return float(json.loads(r.stdout)['format']['duration'])

def apply_freeze_zoom(input_path, output_path, normal_dur, freeze_dur):
    v_dur = get_duration(input_path)
    segments = []
    t = 0.0
    seg_idx = 0
    concat_list = os.path.join(os.path.dirname(output_path), 'freeze_concat.txt')
    while t < v_dur:
        end_normal = min(t + normal_dur, v_dur)
        seg_normal = os.path.join(os.path.dirname(output_path), f'seg_n_{seg_idx}.mp4')
        subprocess.run([
            'ffmpeg','-y',
            '-ss',str(t),'-t',str(end_normal-t),
            '-i',input_path,
            '-c:v','libx264','-c:a','aac',
            '-avoid_negative_ts','make_zero',
            seg_normal
        ], capture_output=True)
        segments.append(seg_normal)
        seg_idx += 1
        t = end_normal
        if t >= v_dur:
            break
        freeze_t = max(0, end_normal - 0.04)
        seg_freeze = os.path.join(os.path.dirname(output_path), f'seg_f_{seg_idx}.mp4')
        subprocess.run([
            'ffmpeg','-y',
            '-ss',str(freeze_t),'-t','0.1',
            '-i',input_path,
            '-loop','1',
            '-t',str(freeze_dur),
            '-vf',f'zoompan=z=\'min(zoom+0.008,1.25)\':d={int(freeze_dur*25)}:x=iw/2-(iw/zoom/2):y=ih/2-(ih/zoom/2):s=1280x720',
            '-c:v','libx264','-r','25',
            '-an',
            seg_freeze
        ], capture_output=True)
        segments.append(seg_freeze)
        seg_idx += 1
    with open(concat_list,'w') as f:
        for s in segments:
            f.write(f"file '{s}'\n")
    subprocess.run([
        'ffmpeg','-y',
        '-f','concat','-safe','0',
        '-i',concat_list,
        '-c:v','libx264','-an',
        output_path
    ], capture_output=True)

def process_video(job_id, video_chunks_paths, audio_path):
    try:
        jobs[job_id]['status'] = 'processing'
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)
        total_audio_dur = get_duration(audio_path)
        chunk_count = len(video_chunks_paths)
        audio_chunk_dur = total_audio_dur / chunk_count
        merged_chunks = []
        for i, vchunk_path in enumerate(video_chunks_paths):
            audio_slice = os.path.join(job_dir, f'audio_{i}.wav')
            subprocess.run([
                'ffmpeg','-y',
                '-i',audio_path,
                '-ss',str(i*audio_chunk_dur),
                '-t',str(audio_chunk_dur),
                audio_slice
            ], capture_output=True)
            frozen_path = os.path.join(job_dir, f'frozen_{i}.mp4')
            apply_freeze_zoom(vchunk_path, frozen_path, NORMAL_DUR, FREEZE_DUR)
            frozen_dur = get_duration(frozen_path)
            merged_path = os.path.join(job_dir, f'merged_{i}.mp4')
            speed = frozen_dur / audio_chunk_dur
            subprocess.run([
                'ffmpeg','-y',
                '-i',frozen_path,
                '-i',audio_slice,
                '-filter_complex',f'[0:v]setpts={1/speed}*PTS[v]',
                '-map','[v]',
                '-map','1:a',
                '-c:v','libx264','-c:a','aac',
                '-shortest',
                merged_path
            ], capture_output=True)
            merged_chunks.append(merged_path)
            jobs[job_id]['progress'] = int(((i+1)/chunk_count)*90)
        concat_list = os.path.join(job_dir,'concat.txt')
        with open(concat_list,'w') as f:
            for p in merged_chunks:
                f.write(f"file '{p}'\n")
        final_path = os.path.join(job_dir,'final_output.mp4')
        subprocess.run([
            'ffmpeg','-y',
            '-f','concat','-safe','0',
            '-i',concat_list,
            '-c','copy',
            final_path
        ], capture_output=True)
        jobs[job_id].update({'status':'done','progress':100,'output':final_path})
    except Exception as e:
        jobs[job_id].update({'status':'error','error':str(e)})

@app.route('/upload', methods=['POST'])
def upload():
    try:
        job_id = str(uuid.uuid4())
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)
        os.makedirs(job_dir, exist_ok=True)
        audio_file = request.files.get('audio')
        if not audio_file:
            return jsonify({'error':'Audio missing'}),400
        audio_path = os.path.join(job_dir,'audio.wav')
        audio_file.save(audio_path)
        chunks = []
        i = 0
        while f'chunk_{i}' in request.files:
            cp = os.path.join(job_dir,f'chunk_{i}.mp4')
            request.files[f'chunk_{i}'].save(cp)
            chunks.append(cp)
            i += 1
        if not chunks:
            return jsonify({'error':'No chunks'}),400
        jobs[job_id] = {'status':'queued','progress':0,'output':None,'error':None}
        t = threading.Thread(target=process_video,args=(job_id,chunks,audio_path))
        t.daemon = True
        t.start()
        return jsonify({'job_id':job_id}),200
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.route('/status/<job_id>')
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error':'Not found'}),404
    return jsonify({'status':job['status'],'progress':job['progress'],'error':job.get('error')})

@app.route('/download/<job_id>')
def download(job_id):
    job = jobs.get(job_id)
    if not job or job['status']!='done':
        return jsonify({'error':'Not ready'}),404
    return send_file(job['output'],mimetype='video/mp4',as_attachment=True,download_name='recap_final.mp4')

@app.route('/health')
def health():
    return jsonify({'status':'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)))
