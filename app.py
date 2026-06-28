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
    r = subprocess.run(
        ['ffprobe','-v','quiet','-print_format','json','-show_format',path],
        capture_output=True, text=True
    )
    return float(json.loads(r.stdout)['format']['duration'])

def get_audio_peaks(audio_path, job_dir):
    """Convert audio to mono WAV and detect silence/peak boundaries"""
    wav_path = os.path.join(job_dir, 'audio_mono.wav')
    subprocess.run([
        'ffmpeg', '-y', '-i', audio_path,
        '-ac', '1', '-ar', '8000',  # mono, 8kHz for fast processing
        wav_path
    ], capture_output=True)

    with wave.open(wav_path, 'rb') as wf:
        frames = wf.readframes(wf.getnframes())
        sr = wf.getframerate()

    samples = struct.unpack(f'{len(frames)//2}h', frames)
    total_dur = len(samples) / sr

    # Calculate RMS energy every 0.5s window
    window = int(sr * 0.5)
    energies = []
    for i in range(0, len(samples) - window, window):
        chunk = samples[i:i+window]
        rms = (sum(s*s for s in chunk) / len(chunk)) ** 0.5
        energies.append(rms)

    # Find max energy for normalization
    max_e = max(energies) if energies else 1
    norm = [e / max_e for e in energies]

    # Build freeze points: freeze where energy is LOW (natural pause)
    # Every ~6s, pick lowest energy window for freeze
    SEGMENT_SEC = 6.0
    FREEZE_DUR = 2.0
    seg_windows = int(SEGMENT_SEC / 0.5)

    freeze_points = []  # list of (video_time, freeze_dur)
    audio_dur = total_dur

    t = 0.0
    while t < audio_dur - SEGMENT_SEC:
        seg_start_idx = int(t / 0.5)
        seg_end_idx = min(seg_start_idx + seg_windows, len(norm))
        seg = norm[seg_start_idx:seg_end_idx]
        if seg:
            # Find lowest energy point in this segment (natural pause)
            min_idx = seg.index(min(seg))
            freeze_t = t + min_idx * 0.5
            freeze_points.append((freeze_t, FREEZE_DUR))
        t += SEGMENT_SEC

    return freeze_points, audio_dur

def build_video_with_freezes(video_path, audio_path, output_path, job_dir, freeze_points, audio_dur):
    """
    Build final video:
    1. Scale video speed to match audio duration + freeze durations
    2. Insert freeze+zoomIn frames at calculated points
    3. Merge with audio
    """
    v_dur = get_duration(video_path)
    total_freeze = sum(fd for _, fd in freeze_points)
    # Net video play time = audio_dur - total_freeze_time
    net_play_time = audio_dur - total_freeze
    if net_play_time <= 0:
        net_play_time = audio_dur * 0.7

    # Speed factor to fit video into net_play_time
    speed = v_dur / net_play_time
    # Clamp speed between 0.5x and 2x
    speed = max(0.5, min(2.0, speed))

    segments = []
    seg_idx = 0
    prev_vt = 0.0  # video time tracker
    prev_at = 0.0  # audio time tracker

    for freeze_at, freeze_dur in sorted(freeze_points):
        # Normal play segment before freeze
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

        # Freeze + zoom segment
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

    # Remaining video after last freeze
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

    # Concat all segments
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

    # Merge with audio
    subprocess.run([
        'ffmpeg', '-y',
        '-i', video_only,
        '-i', audio_path,
        '-c:v', 'copy', '-c:a', 'aac',
        '-shortest',
        output_path
    ], capture_output=True)


def process_video(job_id, video_path, audio_path):
    try:
        jobs[job_id]['status'] = 'processing'
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)

        # Step 1: Analyze audio for natural freeze points
        jobs[job_id]['progress'] = 10
        freeze_points, audio_dur = get_audio_peaks(audio_path, job_dir)
        jobs[job_id]['progress'] = 25

        # Step 2: Build video with freeze+zoom + audio sync
        final_path = os.path.join(job_dir, 'final_output.mp4')
        build_video_with_freezes(
            video_path, audio_path, final_path,
            job_dir, freeze_points, audio_dur
        )
        jobs[job_id]['progress'] = 95

        if not os.path.exists(final_path):
            raise Exception('Output file not created')

        jobs[job_id].update({'status': 'done', 'progress': 100, 'output': final_path})

    except Exception as e:
        jobs[job_id].update({'status': 'error', 'error': str(e)})


@app.route('/upload', methods=['POST'])
def upload():
    try:
        job_id = str(uuid.uuid4())
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)
        os.makedirs(job_dir, exist_ok=True)

        video_file = request.files.get('video')
        audio_file = request.files.get('audio')
        if not video_file or not audio_file:
            return jsonify({'error': 'Video or audio missing'}), 400

        video_path = os.path.join(job_dir, 'video.mp4')
        audio_path = os.path.join(job_dir, 'audio.wav')
        video_file.save(video_path)
        audio_file.save(audio_path)

        jobs[job_id] = {'status': 'queued', 'progress': 0, 'output': None, 'error': None}

        t = threading.Thread(target=process_video, args=(job_id, video_path, audio_path))
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
