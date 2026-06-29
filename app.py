import os
import uuid
import subprocess
import threading
import json
import struct
import wave
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = '/tmp/recap_jobs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
jobs = {}

def get_duration(path):
    try:
        r = subprocess.run(
            ['ffprobe','-v','quiet','-print_format','json','-show_format',path],
            capture_output=True, text=True
        )
        data = json.loads(r.stdout)
        return float(data['format']['duration'])
    except Exception:
        try:
            r = subprocess.run(
                ['ffprobe','-v','quiet','-print_format','json','-show_streams',path],
                capture_output=True, text=True
            )
            data = json.loads(r.stdout)
            for stream in data.get('streams', []):
                if 'duration' in stream:
                    return float(stream['duration'])
        except Exception:
            pass
        return 600.0


def convert_to_mp4(input_path, output_path):
    result = subprocess.run([
        'ffmpeg', '-y', '-i', input_path,
        '-c:v', 'libx264', '-preset', 'fast',
        '-c:a', 'aac',
        output_path
    ], capture_output=True)
    return result.returncode == 0

def get_audio_peaks(audio_path, job_dir):
    wav_path = os.path.join(job_dir, 'audio_mono.wav')
    subprocess.run([
        'ffmpeg', '-y', '-i', audio_path,
        '-ac', '1', '-ar', '8000',
        wav_path
    ], capture_output=True)

    with wave.open(wav_path, 'rb') as wf:
        frames = wf.readframes(wf.getnframes())
        sr = wf.getframerate()

    samples = struct.unpack(f'{len(frames)//2}h', frames)
    total_dur = len(samples) / sr

    window = int(sr * 0.5)
    energies = []
    for i in range(0, len(samples) - window, window):
        chunk = samples[i:i+window]
        rms = (sum(s*s for s in chunk) / len(chunk)) ** 0.5
        energies.append(rms)

    max_e = max(energies) if energies else 1
    norm = [e / max_e for e in energies]

    SEGMENT_SEC = 6.0
    FREEZE_DUR = 2.0
    seg_windows = int(SEGMENT_SEC / 0.5)

    freeze_points = []
    audio_dur = total_dur

    t = 0.0
    while t < audio_dur - SEGMENT_SEC:
        seg_start_idx = int(t / 0.5)
        seg_end_idx = min(seg_start_idx + seg_windows, len(norm))
        seg = norm[seg_start_idx:seg_end_idx]
        if seg:
            min_idx = seg.index(min(seg))
            freeze_t = t + min_idx * 0.5
            freeze_points.append((freeze_t, FREEZE_DUR))
        t += SEGMENT_SEC

    return freeze_points, audio_dur

def build_video_with_freezes(video_path, audio_path, output_path, job_dir, freeze_points, audio_dur):
    v_dur = get_duration(video_path)
    total_freeze = sum(fd for _, fd in freeze_points)
    net_play_time = audio_dur - total_freeze
    if net_play_time <= 0:
        net_play_time = audio_dur * 0.7

    speed = v_dur / net_play_time
    speed = max(0.5, min(2.0, speed))

    segments = []
    seg_idx = 0
    prev_vt = 0.0
    prev_at = 0.0

    for freeze_at, freeze_dur in sorted(freeze_points):
        normal_audio = freeze_at - prev_at
        if normal_audio > 0.1:
            normal_video = normal_audio * speed
            seg_path = os.path.join(job_dir, f'seg_n_{seg_idx}.mp4')
            subprocess.run([
                'ffmpeg', '-y',
                '-ss', str(prev_vt), '-t', str(normal_video),
                '-i', video_path,
                '-vf', f'setpts={1/speed}*PTS',
                '-an', '-c:v', 'libx264', '-preset', 'fast',
                seg_path
            ], capture_output=True)
            segments.append(seg_path)
            seg_idx += 1
            prev_vt += normal_video

        freeze_frame_t = prev_vt
        seg_freeze = os.path.join(job_dir, f'seg_f_{seg_idx}.mp4')
        fps = 25
        frames = int(freeze_dur * fps)
        subprocess.run([
            'ffmpeg', '-y',
            '-ss', str(max(0, freeze_frame_t - 0.04)),
            '-i', video_path,
            '-vframes', '1',
            '-q:v', '2',
            os.path.join(job_dir, f'freeze_{seg_idx}.jpg')
        ], capture_output=True)

        subprocess.run([
            'ffmpeg', '-y',
            '-loop', '1',
            '-i', os.path.join(job_dir, f'freeze_{seg_idx}.jpg'),
            '-t', str(freeze_dur),
            '-vf', f'zoompan=z=\'min(zoom+0.006,1.3)\':d={frames}:x=iw/2-(iw/zoom/2):y=ih/2-(ih/zoom/2):s=1280x720,fps={fps}',
            '-c:v', 'libx264', '-preset', 'fast',
            '-an',
            seg_freeze
        ], capture_output=True)
        segments.append(seg_freeze)
        seg_idx += 1
        prev_at = freeze_at + freeze_dur

    remaining_audio = audio_dur - prev_at
    if remaining_audio > 0.1:
        remaining_video = remaining_audio * speed
        seg_path = os.path.join(job_dir, f'seg_n_{seg_idx}.mp4')
        subprocess.run([
            'ffmpeg', '-y',
            '-ss', str(prev_vt), '-t', str(remaining_video),
            '-i', video_path,
            '-vf', f'setpts={1/speed}*PTS',
            '-an', '-c:v', 'libx264', '-preset', 'fast',
            seg_path
        ], capture_output=True)
        segments.append(seg_path)

    concat_list = os.path.join(job_dir, 'concat.txt')
    with open(concat_list, 'w') as f:
        for s in segments:
            if os.path.exists(s):
                f.write(f"file '{s}'\n")

    video_only = os.path.join(job_dir, 'video_only.mp4')
    subprocess.run([
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0',
        '-i', concat_list,
        '-c:v', 'libx264', '-preset', 'fast',
        '-an', video_only
    ], capture_output=True)

    subprocess.run([
        'ffmpeg', '-y',
        '-i', video_only,
        '-i', audio_path,
        '-c:v', 'copy', '-c:a', 'aac',
        '-shortest',
        output_path
    ], capture_output=True)


def split_audio_for_chunks(audio_path, chunk_paths, chunk_durations, job_dir):
    """Split full audio proportionally across video chunks"""
    try:
        # Get total audio duration
        r = subprocess.run(
            ['ffprobe','-v','quiet','-print_format','json','-show_format', audio_path],
            capture_output=True, text=True
        )
        total_audio_dur = float(json.loads(r.stdout)['format']['duration'])
        
        total_video_dur = sum(chunk_durations)
        audio_start = 0.0
        audio_parts = []
        
        for i, (chunk_path, chunk_dur) in enumerate(zip(chunk_paths, chunk_durations)):
            # Proportional audio duration for this chunk
            audio_dur_for_chunk = (chunk_dur / total_video_dur) * total_audio_dur
            audio_chunk_path = os.path.join(job_dir, f'audio_chunk_{i}.wav')
            subprocess.run([
                'ffmpeg', '-y',
                '-ss', str(audio_start),
                '-t', str(audio_dur_for_chunk),
                '-i', audio_path,
                audio_chunk_path
            ], capture_output=True)
            audio_parts.append((audio_chunk_path, audio_dur_for_chunk))
            audio_start += audio_dur_for_chunk
        
        return audio_parts
    except Exception as e:
        raise Exception(f"Audio split error: {e}")


def process_chunked_video(job_id, chunk_paths, chunk_durations, audio_path):
    """Process multiple video chunks with proportional audio, then concat"""
    try:
        jobs[job_id]['status'] = 'processing'
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)
        n_chunks = len(chunk_paths)
        
        jobs[job_id]['progress'] = 5
        jobs[job_id]['stage'] = f'အသံဖိုင် ခွဲနေသည် ({n_chunks} chunks)...'
        
        # Split audio proportionally
        audio_parts = split_audio_for_chunks(audio_path, chunk_paths, chunk_durations, job_dir)
        
        processed_chunks = []
        
        for i, (chunk_path, (audio_chunk_path, audio_chunk_dur)) in enumerate(zip(chunk_paths, audio_parts)):
            chunk_dir = os.path.join(job_dir, f'chunk_{i}')
            os.makedirs(chunk_dir, exist_ok=True)
            
            base_progress = 5 + int((i / n_chunks) * 80)
            jobs[job_id]['progress'] = base_progress
            jobs[job_id]['stage'] = f'Chunk {i+1}/{n_chunks} - အသံတိုင်းတာနေသည်...'
            
            # Analyze audio peaks for this chunk
            freeze_points, _ = get_audio_peaks(audio_chunk_path, chunk_dir)
            
            jobs[job_id]['progress'] = base_progress + int(10 / n_chunks)
            jobs[job_id]['stage'] = f'Chunk {i+1}/{n_chunks} - ဗီဒီယို ပြင်ဆင်နေသည်...'
            
            # Build video for this chunk
            chunk_output = os.path.join(job_dir, f'chunk_output_{i}.mp4')
            build_video_with_freezes(
                chunk_path, audio_chunk_path, chunk_output,
                chunk_dir, freeze_points, audio_chunk_dur
            )
            
            if os.path.exists(chunk_output):
                processed_chunks.append(chunk_output)
            else:
                raise Exception(f'Chunk {i} output not created')
        
        # Concat all processed chunks
        jobs[job_id]['progress'] = 88
        jobs[job_id]['stage'] = 'Chunks အားလုံး ပေါင်းစပ်နေသည်...'
        
        final_concat_list = os.path.join(job_dir, 'final_concat.txt')
        with open(final_concat_list, 'w') as f:
            for cp in processed_chunks:
                f.write(f"file '{cp}'\n")
        
        final_path = os.path.join(job_dir, 'final_output.mp4')
        subprocess.run([
            'ffmpeg', '-y',
            '-f', 'concat', '-safe', '0',
            '-i', final_concat_list,
            '-c', 'copy',
            final_path
        ], capture_output=True)
        
        jobs[job_id]['progress'] = 98
        
        if not os.path.exists(final_path):
            raise Exception('Final output file not created')
        
        jobs[job_id].update({
            'status': 'done',
            'progress': 100,
            'stage': 'ပြီးဆုံးပြီ!',
            'output': final_path
        })

    except Exception as e:
        jobs[job_id].update({'status': 'error', 'error': str(e)})


def process_video(job_id, video_path, audio_path):
    """Single video (no chunks) - original behavior"""
    try:
        jobs[job_id]['status'] = 'processing'
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)

        jobs[job_id]['progress'] = 10
        jobs[job_id]['stage'] = 'အသံဖိုင် စစ်ဆေးနေသည်...'
        freeze_points, audio_dur = get_audio_peaks(audio_path, job_dir)
        jobs[job_id]['progress'] = 25
        
        jobs[job_id]['stage'] = 'ဗီဒီယို ပြင်ဆင်နေသည်...'
        final_path = os.path.join(job_dir, 'final_output.mp4')
        build_video_with_freezes(
            video_path, audio_path, final_path,
            job_dir, freeze_points, audio_dur
        )
        jobs[job_id]['progress'] = 95

        if not os.path.exists(final_path):
            raise Exception('Output file not created')

        jobs[job_id].update({
            'status': 'done',
            'progress': 100,
            'stage': 'ပြီးဆုံးပြီ!',
            'output': final_path
        })

    except Exception as e:
        jobs[job_id].update({'status': 'error', 'error': str(e)})


@app.route('/upload', methods=['POST'])
def upload():
    try:
        job_id = str(uuid.uuid4())
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)
        os.makedirs(job_dir, exist_ok=True)

        audio_file = request.files.get('audio')
        if not audio_file:
            return jsonify({'error': 'Audio missing'}), 400

        audio_path = os.path.join(job_dir, 'audio.wav')
        audio_file.save(audio_path)

        # Check for chunked upload (chunk_0, chunk_1, ...) vs single video
        chunk_files = []
        i = 0
        while True:
            cf = request.files.get(f'chunk_{i}')
            if cf is None:
                break
            webm_path = os.path.join(job_dir, f'chunk_{i}.webm')
            mp4_path = os.path.join(job_dir, f'chunk_{i}.mp4')
            cf.save(webm_path)
            # Convert webm to mp4 for ffmpeg compatibility
            if convert_to_mp4(webm_path, mp4_path):
                chunk_files.append(mp4_path)
            else:
                chunk_files.append(webm_path)
            i += 1

        # Also check for single 'video' field (backward compat)
        video_file = request.files.get('video')
        
        jobs[job_id] = {
            'status': 'queued',
            'progress': 0,
            'stage': 'Queue တွင် စောင့်နေသည်...',
            'output': None,
            'error': None
        }

        if chunk_files:
            # Get durations for each chunk
            chunk_durations = []
            for cp in chunk_files:
                try:
                    dur = get_duration(cp)
                except:
                    dur = 600.0  # fallback 10min
                chunk_durations.append(dur)
            
            t = threading.Thread(
                target=process_chunked_video,
                args=(job_id, chunk_files, chunk_durations, audio_path)
            )
        elif video_file:
            video_path = os.path.join(job_dir, 'video.mp4')
            video_file.save(video_path)
            t = threading.Thread(target=process_video, args=(job_id, video_path, audio_path))
        else:
            return jsonify({'error': 'Video or chunks missing'}), 400

        t.daemon = True
        t.start()

        return jsonify({'job_id': job_id}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/status/<job_id>')
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'status': job['status'],
        'progress': job['progress'],
        'stage': job.get('stage', ''),
        'error': job.get('error')
    })


@app.route('/download/<job_id>')
def download(job_id):
    job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'Not ready'}), 404
    return send_file(
        job['output'],
        mimetype='video/mp4',
        as_attachment=True,
        download_name='recap_final.mp4'
    )


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
