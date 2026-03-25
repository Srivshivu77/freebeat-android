from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import requests
import os
import tempfile
import traceback
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

app = Flask(__name__)
CORS(app)

# ── Config ─────────────────────────────────────────────────────────────
WEB_CLIENT_ID = "224375227718-l5uid2p3cm61bgofsc01skoqn217csqi.apps.googleusercontent.com"

# ── Cookie Loading ─────────────────────────────────────────────────────
def get_cookie_path():
    """Extracts Netscape cookies from YOUTUBE_COOKIES env var or local file."""
    content = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if content and "# Netscape HTTP Cookie File" in content:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.write(content)
        tmp.flush()
        tmp.close()
        return tmp.name
        
    local_path = os.path.join(os.path.dirname(__file__), 'cookies.txt')
    if os.path.exists(local_path):
        return local_path
        
    return None

_active_cookie_path = get_cookie_path()

# ── yt-dlp Options (From User's Original Working Code) ─────────────────
SEARCH_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': True,
    'playlistend': 20,
}

STREAM_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'format': 'bestaudio/best',
}

if _active_cookie_path:
    SEARCH_OPTS['cookiefile'] = _active_cookie_path
    STREAM_OPTS['cookiefile'] = _active_cookie_path

def extract_best_audio(vid_id):
    """User's proven extraction logic."""
    with yt_dlp.YoutubeDL(STREAM_OPTS) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={vid_id}",
            download=False
        )
        formats = info.get('formats', [])

        # Prefer audio-only formats
        audio_only = [
            f for f in formats
            if f.get('vcodec') in ('none', None) and f.get('acodec') not in ('none', None) and f.get('url')
        ]
        if not audio_only:
            audio_only = [f for f in formats if f.get('url')]

        if not audio_only:
            raise Exception("No playable formats found")

        best = sorted(audio_only, key=lambda f: f.get('abr') or 0, reverse=True)[0]
        return best, info

# ── Routes ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'source': 'youtube', 'cookies_loaded': bool(_active_cookie_path)})

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'source': 'youtube',
        'yt_dlp': yt_dlp.version.__version__,
        'cookies_loaded': bool(_active_cookie_path)
    })

@app.route('/auth/google', methods=['POST'])
def auth_google():
    try:
        data = request.get_json()
        token = data.get('token', '')
        if not token:
            return jsonify({'error': 'no token'}), 400

        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), WEB_CLIENT_ID)
        return jsonify({
            'success': True,
            'user_id': idinfo['sub'],
            'email': idinfo.get('email', ''),
            'name': idinfo.get('name', ''),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 401

@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])

    try:
        with yt_dlp.YoutubeDL(SEARCH_OPTS) as ydl:
            # Using the original ' music' appending trick you provided
            info = ydl.extract_info(f"ytsearch20:{q} music", download=False)
            results = []
            for entry in info.get('entries', []):
                duration = entry.get('duration') or 0
                if duration > 600:  # skip anything over 10 mins
                    continue
                results.append({
                    'id':       entry.get('id', ''),
                    'title':    entry.get('title', ''),
                    'channel':  entry.get('uploader') or entry.get('channel', ''),
                    'duration': duration,
                    'thumb':    f"https://i.ytimg.com/vi/{entry.get('id')}/mqdefault.jpg",
                    'source':   'youtube',
                })
            return jsonify(results[:15])
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/trending')
def trending():
    lang = request.args.get('lang', 'hindi')
    queries = {
        'hindi': 'Hindi songs 2025 trending',
        'english': 'English pop hits 2025',
        'punjabi': 'Punjabi songs 2025 hits',
        'tamil': 'Tamil songs 2025 trending',
        'telugu': 'Telugu songs 2025 hits',
    }
    q = queries.get(lang, 'trending music 2025')
    
    try:
        with yt_dlp.YoutubeDL(SEARCH_OPTS) as ydl:
            info = ydl.extract_info(f"ytsearch20:{q}", download=False)
            results = []
            for entry in info.get('entries', []):
                dur = entry.get('duration') or 0
                if dur > 600: continue
                vid_id = entry.get('id', '')
                if not vid_id: continue
                
                results.append({
                    'id': vid_id,
                    'title': entry.get('title', ''),
                    'channel': entry.get('uploader') or entry.get('channel', ''),
                    'duration': dur,
                    'thumb': f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
                    'source': 'youtube',
                })
        return jsonify(results[:15])
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/stream')
def stream():
    vid_id = request.args.get('id', '').strip()
    if not vid_id: return jsonify({'error': 'no id'}), 400
    
    try:
        best, info = extract_best_audio(vid_id)
        return jsonify({
            'title': info.get('title', ''),
            'channel': info.get('uploader', ''),
            'thumb': info.get('thumbnail', ''),
            'duration': info.get('duration', 0),
            'url': f"/proxy?id={vid_id}",
            'source': 'youtube',
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/proxy')
def proxy():
    vid_id = request.args.get('id', '').strip()
    if not vid_id: return jsonify({'error': 'no id'}), 400
    
    try:
        best, _ = extract_best_audio(vid_id)
        yt_url = best['url']
        ext = best.get('ext', 'webm')
        ct_map = {'webm':'audio/webm','m4a':'audio/mp4','mp4':'audio/mp4','ogg':'audio/ogg','opus':'audio/ogg'}
        content_type = ct_map.get(ext, 'audio/webm')

        req_headers = {
            'User-Agent': 'com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip',
            'Accept': '*/*',
        }
        if request.headers.get('Range'):
            req_headers['Range'] = request.headers['Range']

        r = requests.get(yt_url, headers=req_headers, stream=True, timeout=30)

        resp_headers = {
            'Content-Type': content_type,
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*',
        }
        if 'Content-Length' in r.headers: resp_headers['Content-Length'] = r.headers['Content-Length']
        if 'Content-Range'  in r.headers: resp_headers['Content-Range']  = r.headers['Content-Range']

        return Response(stream_with_context(r.iter_content(chunk_size=16384)), status=r.status_code, headers=resp_headers)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/lyrics')
def lyrics():
    title = request.args.get('title', '').strip()
    artist = request.args.get('artist', '').strip()
    if not title: return jsonify({'error': 'title required'}), 400
    
    try:
        r = requests.get('https://lrclib.net/api/search', params={'track_name': title, 'artist_name': artist}, headers={'User-Agent': 'FreeBeat/1.0'}, timeout=8)
        r.raise_for_status()
        results = r.json()
        if not results: return jsonify({'lyrics': None, 'synced': False})
        
        best = results[0]
        synced, plain = best.get('syncedLyrics', ''), best.get('plainLyrics', '')
        
        if synced: return jsonify({'lyrics': synced, 'synced': True, 'source': 'lrclib'})
        elif plain: return jsonify({'lyrics': plain, 'synced': False, 'source': 'lrclib'})
        return jsonify({'lyrics': None, 'synced': False})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"🎵 FreeBeat YouTube backend on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)