from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import requests
import os
import tempfile
import traceback
import json
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

app = Flask(__name__)
CORS(app)

# ── Config ─────────────────────────────────────────────────────────────
WEB_CLIENT_ID = "224375227718-l5uid2p3cm61bgofsc01skoqn217csqi.apps.googleusercontent.com"

# ── Cookie/token storage per user ─────────────────────────────────────
# Maps google_user_id -> cookie file path
user_cookie_files = {}

# ── Fallback server cookie (from env) ─────────────────────────────────
SERVER_COOKIE_PATH = None

def setup_server_cookies():
    global SERVER_COOKIE_PATH
    content = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if content:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.write(content); tmp.flush(); tmp.close()
        SERVER_COOKIE_PATH = tmp.name
        print("🍪 Server cookies loaded from env")
    else:
        local = os.path.join(os.path.dirname(__file__), 'cookies.txt')
        if os.path.exists(local):
            SERVER_COOKIE_PATH = local
            print("🍪 Server cookies loaded from file")
        else:
            print("⚠️  No server cookies — will use Android VR client")

setup_server_cookies()

def get_cookie_path(google_token=None):
    """Get the best available cookie file."""
    # If user sent a google token, verify and use their identity
    if google_token:
        try:
            idinfo = id_token.verify_oauth2_token(
                google_token,
                google_requests.Request(),
                WEB_CLIENT_ID
            )
            user_id = idinfo['sub']
            if user_id in user_cookie_files:
                return user_cookie_files[user_id]
        except Exception as e:
            print(f"Token verify failed: {e}")

    # Fall back to server cookies
    if SERVER_COOKIE_PATH:
        return SERVER_COOKIE_PATH

    return None

# ── yt-dlp strategies ──────────────────────────────────────────────────
AUDIO_FORMAT = 'bestaudio[vcodec=none][acodec!=none]/bestaudio'

STRATEGIES = [
    {'extractor_args': {'youtube': {'player_client': ['android_vr']}}},
    {'extractor_args': {'youtube': {'player_client': ['android']}}},
    {'extractor_args': {'youtube': {'player_client': ['tv_embedded']}}},
    {'extractor_args': {'youtube': {'player_client': ['web']}}},
    {},
]

def make_opts(cookie_path=None, extra=None):
    opts = {
        'quiet':       True,
        'no_warnings': True,
        'format':      AUDIO_FORMAT,
    }
    if cookie_path:
        opts['cookiefile'] = cookie_path
    if extra:
        opts.update(extra)
    return opts

def get_audio(vid_id, cookie_path=None):
    errors = []
    for i, strategy in enumerate(STRATEGIES):
        try:
            opts = make_opts(cookie_path, strategy)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info  = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={vid_id}",
                    download=False
                )
                fmts  = info.get('formats', [])
                audio = [
                    f for f in fmts
                    if f.get('vcodec') in ('none', None, '')
                    and f.get('acodec') not in ('none', None, '')
                    and f.get('url')
                ]
                if not audio:
                    audio = [f for f in fmts if f.get('url')]
                if not audio:
                    errors.append(f"S{i+1}: no formats")
                    continue
                best = sorted(audio, key=lambda f: f.get('abr') or 0, reverse=True)[0]
                print(f"✅ Strategy {i+1} | ext={best.get('ext')} abr={best.get('abr')}")
                return best, info
        except Exception as e:
            errors.append(f"S{i+1}: {str(e)[:100]}")
            print(f"❌ Strategy {i+1}: {str(e)[:100]}")
    raise Exception("All strategies failed:\n" + "\n".join(errors))

def get_token_from_request():
    return request.headers.get('X-Google-Token') or request.args.get('token')

# ── Routes ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'source': 'youtube'})

@app.route('/health')
def health():
    return jsonify({
        'status':        'ok',
        'source':        'youtube',
        'yt_dlp':        yt_dlp.version.__version__,
        'server_cookies': bool(SERVER_COOKIE_PATH),
    })

@app.route('/auth/google', methods=['POST'])
def auth_google():
    """
    Receive Google ID token from Android app.
    Verify it, store user info, return success.
    """
    try:
        data  = request.get_json()
        token = data.get('token', '')
        if not token:
            return jsonify({'error': 'no token'}), 400

        # Verify the token with Google
        idinfo = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            WEB_CLIENT_ID
        )

        user_id    = idinfo['sub']
        user_email = idinfo.get('email', '')
        user_name  = idinfo.get('name', '')

        print(f"✅ User authenticated: {user_email}")

        return jsonify({
            'success':  True,
            'user_id':  user_id,
            'email':    user_email,
            'name':     user_name,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 401

@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    token       = get_token_from_request()
    cookie_path = get_cookie_path(token)
    try:
        opts = {
            'quiet': True, 'no_warnings': True,
            'extract_flat': True, 'playlistend': 20,
        }
        if cookie_path:
            opts['cookiefile'] = cookie_path

        with yt_dlp.YoutubeDL(opts) as ydl:
            info    = ydl.extract_info(f"ytsearch20:{q}", download=False)
            results = []
            for entry in info.get('entries', []):
                dur    = entry.get('duration') or 0
                if dur > 600: continue
                vid_id = entry.get('id', '')
                if not vid_id: continue
                results.append({
                    'id':       vid_id,
                    'title':    entry.get('title', ''),
                    'channel':  entry.get('uploader') or entry.get('channel', ''),
                    'duration': dur,
                    'thumb':    f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
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
        'hindi':   'Hindi songs 2025 trending',
        'english': 'English pop hits 2025',
        'punjabi': 'Punjabi songs 2025 hits',
        'tamil':   'Tamil songs 2025 trending',
        'telugu':  'Telugu songs 2025 hits',
    }
    q           = queries.get(lang, 'trending music 2025')
    token       = get_token_from_request()
    cookie_path = get_cookie_path(token)
    try:
        opts = {
            'quiet': True, 'no_warnings': True,
            'extract_flat': True, 'playlistend': 20,
        }
        if cookie_path:
            opts['cookiefile'] = cookie_path

        with yt_dlp.YoutubeDL(opts) as ydl:
            info    = ydl.extract_info(f"ytsearch20:{q}", download=False)
            results = []
            for entry in info.get('entries', []):
                dur    = entry.get('duration') or 0
                if dur > 600: continue
                vid_id = entry.get('id', '')
                if not vid_id: continue
                results.append({
                    'id':       vid_id,
                    'title':    entry.get('title', ''),
                    'channel':  entry.get('uploader') or entry.get('channel', ''),
                    'duration': dur,
                    'thumb':    f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
                    'source':   'youtube',
                })
        return jsonify(results[:15])
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/stream')
def stream():
    vid_id = request.args.get('id', '').strip()
    if not vid_id:
        return jsonify({'error': 'no id'}), 400
    token       = get_token_from_request()
    cookie_path = get_cookie_path(token)
    try:
        best, info = get_audio(vid_id, cookie_path)
        ext    = best.get('ext', 'webm')
        ct_map = {'webm':'audio/webm','m4a':'audio/mp4','mp4':'audio/mp4','ogg':'audio/ogg','opus':'audio/ogg'}
        return jsonify({
            'title':    info.get('title', ''),
            'channel':  info.get('uploader', ''),
            'thumb':    info.get('thumbnail', ''),
            'duration': info.get('duration', 0),
            'url':      f"/proxy?id={vid_id}",
            'source':   'youtube',
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/proxy')
def proxy():
    vid_id = request.args.get('id', '').strip()
    if not vid_id:
        return jsonify({'error': 'no id'}), 400
    token       = get_token_from_request()
    cookie_path = get_cookie_path(token)
    try:
        best, _ = get_audio(vid_id, cookie_path)
        yt_url  = best['url']
        ext     = best.get('ext', 'webm')
        ct_map  = {'webm':'audio/webm','m4a':'audio/mp4','mp4':'audio/mp4','ogg':'audio/ogg','opus':'audio/ogg'}
        content_type = ct_map.get(ext, 'audio/webm')

        req_headers = {
            'User-Agent': 'com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip',
            'Accept':     '*/*',
            'Origin':     'https://www.youtube.com',
            'Referer':    'https://www.youtube.com/',
        }
        if request.headers.get('Range'):
            req_headers['Range'] = request.headers['Range']

        r = requests.get(yt_url, headers=req_headers, stream=True, timeout=30)

        resp_headers = {
            'Content-Type':                content_type,
            'Accept-Ranges':               'bytes',
            'Cache-Control':               'no-cache',
            'Access-Control-Allow-Origin': '*',
        }
        if 'Content-Length' in r.headers: resp_headers['Content-Length'] = r.headers['Content-Length']
        if 'Content-Range'  in r.headers: resp_headers['Content-Range']  = r.headers['Content-Range']

        return Response(
            stream_with_context(r.iter_content(chunk_size=16384)),
            status=r.status_code, headers=resp_headers
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/lyrics')
def lyrics():
    title  = request.args.get('title',  '').strip()
    artist = request.args.get('artist', '').strip()
    if not title:
        return jsonify({'error': 'title required'}), 400
    try:
        r = requests.get('https://lrclib.net/api/search',
            params={'track_name': title, 'artist_name': artist},
            headers={'User-Agent': 'FreeBeat/1.0'}, timeout=8)
        r.raise_for_status()
        results = r.json()
        if not results:
            return jsonify({'lyrics': None, 'synced': False})
        best   = results[0]
        synced = best.get('syncedLyrics', '')
        plain  = best.get('plainLyrics', '')
        if synced:
            return jsonify({'lyrics': synced, 'synced': True,  'source': 'lrclib'})
        elif plain:
            return jsonify({'lyrics': plain,  'synced': False, 'source': 'lrclib'})
        else:
            return jsonify({'lyrics': None, 'synced': False})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"🎵 FreeBeat YouTube backend on port {port}")
    print(f"   yt-dlp: {yt_dlp.version.__version__}")
    app.run(host='0.0.0.0', port=port, debug=False)