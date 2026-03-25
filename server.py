from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import requests
import os
import time
import threading
import tempfile
import traceback
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

app = Flask(__name__)
CORS(app)

# ── Config ─────────────────────────────────────────────────────────────
WEB_CLIENT_ID = "224375227718-l5uid2p3cm61bgofsc01skoqn217csqi.apps.googleusercontent.com"
COOKIE_REFRESH_INTERVAL = 6 * 60 * 60  # refresh every 6 hours

# ── Cookie state ───────────────────────────────────────────────────────
_cookie_lock       = threading.Lock()
_cookie_path       = None        # path to current valid cookie file
_cookie_expires_at = 0           # unix timestamp when we should refresh
_cookie_ready      = threading.Event()  # signals that cookies are available

# ── OAuth2 cookie bootstrap ────────────────────────────────────────────

def _write_env_cookies():
    """Write YOUTUBE_COOKIES env var to a temp file. Returns path or None."""
    content = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if not content:
        return None
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    tmp.write(content)
    tmp.flush()
    tmp.close()
    print("🍪 Loaded cookies from YOUTUBE_COOKIES env var")
    return tmp.name

def _refresh_oauth2_cookies():
    """
    Use yt-dlp's built-in OAuth2 flow to get fresh YouTube cookies.
    yt-dlp handles the entire token exchange internally.
    Returns path to cookie file, or None on failure.
    """
    try:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.close()
        cookie_file = tmp.name

        opts = {
            'quiet':        False,
            'no_warnings':  False,
            'cookiefile':   cookie_file,
            # OAuth2 login — yt-dlp manages the token internally
            'username':     'oauth2',
            'password':     '',
            'extractor_args': {'youtube': {'player_client': ['web']}},
        }

        # Just extract a simple public video to trigger the OAuth2 handshake
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(
                'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                download=False
            )

        print("✅ OAuth2 cookie refresh successful")
        return cookie_file

    except Exception as e:
        print(f"❌ OAuth2 cookie refresh failed: {e}")
        return None

def _get_cookie_path():
    """Return current valid cookie path, refreshing if needed."""
    global _cookie_path, _cookie_expires_at

    with _cookie_lock:
        now = time.time()

        # Still valid — return immediately
        if _cookie_path and now < _cookie_expires_at:
            return _cookie_path

        # Try env var first (fastest, no network call)
        env_path = _write_env_cookies()
        if env_path:
            _cookie_path       = env_path
            _cookie_expires_at = now + COOKIE_REFRESH_INTERVAL
            _cookie_ready.set()
            return _cookie_path

        # Try OAuth2 auto-refresh
        print("🔄 Refreshing YouTube cookies via OAuth2...")
        new_path = _refresh_oauth2_cookies()
        if new_path:
            # Delete old cookie file
            if _cookie_path and os.path.exists(_cookie_path):
                try: os.unlink(_cookie_path)
                except: pass
            _cookie_path       = new_path
            _cookie_expires_at = now + COOKIE_REFRESH_INTERVAL
            _cookie_ready.set()
            return _cookie_path

        # No cookies available — return whatever we have (may be None)
        print("⚠️  No cookies available — YouTube may block requests")
        _cookie_ready.set()
        return _cookie_path

def _background_refresh():
    """Background thread — refreshes cookies before they expire."""
    while True:
        time.sleep(60)  # check every minute
        now = time.time()
        with _cookie_lock:
            expires = _cookie_expires_at
        # Refresh 30 minutes before expiry
        if expires > 0 and now >= expires - 1800:
            print("🔄 Background cookie refresh starting...")
            _get_cookie_path()

# Start background refresh thread
threading.Thread(target=_background_refresh, daemon=True).start()

# Initial cookie load on startup (non-blocking)
threading.Thread(target=_get_cookie_path, daemon=True).start()

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

def get_audio(vid_id):
    """Try all strategies with cookies, fall back to no-cookie attempt."""
    cookie_path = _get_cookie_path()
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
                print(f"✅ Strategy {i+1} worked | ext={best.get('ext')} abr={best.get('abr')}")
                return best, info
        except Exception as e:
            errors.append(f"S{i+1}: {str(e)[:120]}")
            print(f"❌ Strategy {i+1}: {str(e)[:120]}")

            # If bot-detected, force an immediate cookie refresh and retry once
            if 'Sign in to confirm' in str(e) and i == 0:
                print("🤖 Bot detected — forcing cookie refresh...")
                with _cookie_lock:
                    global _cookie_expires_at
                    _cookie_expires_at = 0  # force refresh on next call
                cookie_path = _get_cookie_path()

    raise Exception("All strategies failed:\n" + "\n".join(errors))

def get_token_from_request():
    return request.headers.get('X-Google-Token') or request.args.get('token')

# ── Routes ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'source': 'youtube'})

@app.route('/health')
def health():
    cookie_path = _cookie_path
    return jsonify({
        'status':         'ok',
        'source':         'youtube',
        'yt_dlp':         yt_dlp.version.__version__,
        'server_cookies': bool(cookie_path),
        'cookies_expire': int(_cookie_expires_at - time.time()) if _cookie_expires_at else 0,
    })

@app.route('/auth/google', methods=['POST'])
def auth_google():
    """Verify Google ID token from Android app and return user info."""
    try:
        data  = request.get_json()
        token = data.get('token', '')
        if not token:
            return jsonify({'error': 'no token'}), 400

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
            'success': True,
            'user_id': user_id,
            'email':   user_email,
            'name':    user_name,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 401

@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    cookie_path = _get_cookie_path()
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
                if not vid_id or len(vid_id) != 11: continue  # skip truncated IDs
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
    cookie_path = _get_cookie_path()
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
                if not vid_id or len(vid_id) != 11: continue  # skip truncated IDs
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
    if len(vid_id) != 11:
        return jsonify({'error': f'invalid YouTube ID: {vid_id}'}), 400
    try:
        best, info = get_audio(vid_id)
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
    if len(vid_id) != 11:
        return jsonify({'error': f'invalid YouTube ID: {vid_id}'}), 400
    try:
        best, _ = get_audio(vid_id)
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