import os, uuid, subprocess, threading, json
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
UPLOAD_FOLDER = '/tmp/recap_jobs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
jobs = {}

def process_video(job_id, video_chunks_paths, audio_path):
    try:
        jobs[job_id]['status'] = 'processing'
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)
        probe = subprocess.run(['ffprobe','-v','quiet','-print_format','json','-show_format',audio_path],capture_output=True,text=True)
        total_audio_dur = float(json.loads(probe.stdout)['format']['duration'])
        chunk_count = len(video_chunks_paths)
        audio_chunk_dur = total_audio_dur / chunk_count
        merged_chunks = []
        for i, vchunk_path in enumerate(video_chunks_paths):
            audio_slice_path = os.path.join(job_dir, f'audio_{i}.wav')
            merged_path = os.path.join(job_dir, f'merged_{i}.mp4')
            subprocess.run(['ffmpeg','-y','-i',audio_path,'-ss',str(i*audio_chunk_dur),'-t',str(audio_chunk_dur),'-c','copy',audio_slice_path],capture_output=True)
            vprobe = subprocess.run(['ffprobe','-v','quiet','-print_format','json','-show_format',vchunk_path],capture_output=True,text=True)
            vdur = float(json.loads(vprobe.stdout)['format']['duration'])
            subprocess.run(['ffmpeg','-y','-i',vchunk_path,'-i',audio_slice_path,'-filter_complex',f'[0:v]setpts={audio_chunk_dur/vdur}*PTS[v]','-map','[v]','-map','1:a','-c:v','libx264','-c:a','aac','-shortest',merged_path],capture_output=True)
            merged_chunks.append(merged_path)
            jobs[job_id]['progress'] = int(((i+1)/chunk_count)*90)
        concat_path = os.path.join(job_dir,'concat.txt')
        with open(concat_path,'w') as f:
            for p in merged_chunks: f.write(f"file '{p}'\n")
        final_path = os.path.join(job_dir,'final_output.mp4')
        subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',concat_path,'-c','copy',final_path],capture_output=True)
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
        if not audio_file: return jsonify({'error':'Audio missing'}),400
        audio_path = os.path.join(job_dir,'audio.wav')
        audio_file.save(audio_path)
        chunks = []
        i = 0
        while f'chunk_{i}' in request.files:
            cp = os.path.join(job_dir,f'chunk_{i}.mp4')
            request.files[f'chunk_{i}'].save(cp)
            chunks.append(cp)
            i += 1
        if not chunks: return jsonify({'error':'No chunks'}),400
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
    if not job: return jsonify({'error':'Not found'}),404
    return jsonify({'status':job['status'],'progress':job['progress'],'error':job.get('error')})

@app.route('/download/<job_id>')
def download(job_id):
    job = jobs.get(job_id)
    if not job or job['status']!='done': return jsonify({'error':'Not ready'}),404
    return send_file(job['output'],mimetype='video/mp4',as_attachment=True,download_name='recap_final.mp4')

@app.route('/health')
def health():
    return jsonify({'status':'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)))
