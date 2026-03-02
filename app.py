import os
import re
import json
import time
import queue
import shutil
import logging
import zipfile
import threading
import urllib.parse
from urllib.parse import quote
from flask import Flask, render_template, request, Response, send_file, jsonify

app = Flask(__name__)

# Configure minimal logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Base directory setup for native Local Storage
# Save directly to the native Downloads folder
DOWNLOAD_DIR = os.path.expanduser('~/Downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Global dictionary maps a unique client_id to a queue for active SSE streams
progress_queues = {}

def is_youtube_url(url):
    """Ensure the URL structure belongs to YouTube domains to reject arbitrary URLs."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc in ['www.youtube.com', 'youtube.com', 'youtu.be', 'm.youtube.com']
    except Exception:
        return False

def is_playlist_url(url):
    """Detect playlist vs single video utilizing YouTube's list parameter formatting."""
    try:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        return 'list' in query
    except Exception:
        return False

def progress_hook(d, client_id):
    """Hook invoked by yt-dlp; calculates values and routes them to the SSE queue."""
    q = progress_queues.get(client_id)
    if not q: return
        
    if d['status'] == 'downloading':
        try:
            # yt-dlp sometimes outputs ANSI control characters; we strip them cleanly
            clean_ansi = lambda x: re.sub(r'\x1b\[.*?m', '', str(x)).strip() if x else ''
            
            q.put({
                'status': 'downloading',
                'percent': clean_ansi(d.get('_percent_str', '0%')),
                'speed': clean_ansi(d.get('_speed_str', '0 B/s')),
                'eta': clean_ansi(d.get('_eta_str', '00:00')),
                'filename': os.path.basename(d.get('filename', 'video'))
            })
        except Exception as ex:
            logger.error(f"Error parsing progress: {ex}")

def download_task(url, client_id, is_playlist):
    """Background threading logic to decouple long-running downloads from the HTTP interface."""
    import yt_dlp
    import imageio_ffmpeg
    
    q = progress_queues.get(client_id)
    if is_playlist:
        out_dir = os.path.join(DOWNLOAD_DIR, f"temp_playlist_{client_id}")
        os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = DOWNLOAD_DIR
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', # Grab absolute maximum resolution and merge
        'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),                   # Use pip-installed FFmpeg binary
        'merge_output_format': 'mp4',                                         # Output strictly as mp4
        'writesubtitles': False,                                               # Download manually written subtitles
        'writeautomaticsub': False,                                            # Fallback to auto-generated subtitles
        # 'subtitleslangs': ['en', 'en-US', 'en-UK'],                           # Avoid 429 Too Many Requests by requesting specific languages instead of 'all'
        'postprocessors': [
            {'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'},             # Convert subs to SRT for compatibility
            {'key': 'FFmpegEmbedSubtitle'}                                    # Embed subtitles natively into the MP4
        ],
        'outtmpl': os.path.join(out_dir, '%(playlist_index)03d-%(title)s.%(ext)s' if is_playlist else '%(title)s.%(ext)s'),
        'progress_hooks': [lambda d: progress_hook(d, client_id)],
        'noplaylist': not is_playlist, # Force single-video behavior if no list param found
        'extract_flat': False,
        'restrictfilenames': True,     # Automatically sanitize system file names
        'quiet': True,
        'no_warnings': True,
    }

    try:
        q.put({'status': 'info', 'message': 'Fetching metadata...'})
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Metadata fetch helps evaluate early roadblocks (e.g., Geo-restrictions)
            info = ydl.extract_info(url, download=True)
            
        if is_playlist:
            if not info or 'entries' not in info or not info['entries']:
                raise RuntimeError("Playlist is empty or unavailable.")
                
            q.put({'status': 'info', 'message': 'Zipping playlist...'})
            playlist_name = info.get('title', 'playlist').replace('/', '_')
            zip_base_path = os.path.join(DOWNLOAD_DIR, playlist_name)
            
            # Create zip and delete temporary directory
            shutil.make_archive(zip_base_path, 'zip', out_dir)
            shutil.rmtree(out_dir)
            
            q.put({
                'status': 'complete',
                'download_url': None
            })
            
        else:
            q.put({
                'status': 'complete',
                'download_url': None
            })
            
    except Exception as e:
        logger.error(f"yt-dlp error: {str(e)}")
        # Construct highly readable messages
        msg = str(e).lower()
        if 'sign in' in msg or 'age' in msg:
            error_msg = "Age-restricted video. Require authentication."
        elif 'private' in msg:
            error_msg = "Private video. Cannot download."
        elif 'unavailable' in msg:
            error_msg = "Video is unavailable or region-blocked."
        else:
            error_msg = "A download or network extraction failure occurred. Please recheck the URL."
            
        q.put({'status': 'error', 'message': error_msg})

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/start', methods=['POST'])
def start_download():
    # POST endpoint: Validation and threaded delegation
    url = request.form.get('url', '').strip()
    client_id = request.form.get('client_id')
    
    if not url or not client_id:
        return jsonify({'error': 'Missing URL or Session configuration'}), 400
    if not is_youtube_url(url):
        return jsonify({'error': 'Invalid URL. Only youtube.com links are permitted.'}), 400
        
    progress_queues[client_id] = queue.Queue()
    is_playlist = is_playlist_url(url)
    
    # Thread runs isolated logic. We return 200 immediately to UI to wire up SSE listener
    thread = threading.Thread(target=download_task, args=(url, client_id, is_playlist))
    thread.daemon = True
    thread.start()
    return jsonify({'success': True, 'is_playlist': is_playlist})

@app.route('/api/progress/<client_id>')
def progress_stream(client_id):
    # Server-Sent Events Route
    def generate():
        q = progress_queues.get(client_id)
        if not q: return
            
        while True:
            try:
                msg = q.get(timeout=15)
                yield f"data: {json.dumps(msg)}\n\n"
                
                if msg.get('status') in ['complete', 'error']:
                    progress_queues.pop(client_id, None)
                    break
            except queue.Empty:
                yield ": keep-alive\n\n" # Counter acts browser connection drops
    return Response(generate(), mimetype="text/event-stream")

if __name__ == '__main__':
    # Threaded parameter mandatory for bridging SSE and heavy internal routines
    # Using port 5001 as 5000 is often reserved by macOS AirPlay Receiver
    print("\n🚀 Server starting! Click here: http://127.0.0.1:5001")
    app.run(host='0.0.0.0', port=5001, threaded=True)
