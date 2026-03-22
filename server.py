from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS
from Crypto.Cipher import DES
import base64
import requests
import os
import traceback
import re
import tempfile

app = Flask(__name__)
CORS(app)

SAAVN   = "https://www.jiosaavn.com/api.php"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept':     'application/json, text/plain, */*',
    'Referer':    'https://www.jiosaavn.com/',
}

# ── JioSaavn DES decryption ───────────────────────────────────────────
DES_KEY = b'38346591'

def decrypt_url(encrypted: str) -> str:
    enc = encrypted.replace('_', '/').replace('-', '+').replace(' ', '+')
    enc += '=' * (4 - len(enc) % 4)
    raw = base64.b64decode(enc)
    cipher = DES.new(DES_KEY, DES.MODE_ECB)
    decrypted = cipher.decrypt(raw)
    pad = decrypted[-1]
    if isinstance(pad, int) and 1 <= pad <= 8:
        decrypted = decrypted[:-pad]
    url = decrypted.decode('utf-8', errors='ignore').strip()
    url = re.sub(r'_(96|160|48)\.mp4', '_320.mp4', url)
    url = re.sub(r'_(96|160|48)\.mp3', '_320.mp3', url)
    return url

def clean(text):
    if not text: return ''
    text = re.sub(r'&quot;', '"',  text)
    text = re.sub(r'&amp;',  '&',  text)
    text = re.sub(r'&lt;',   '<',  text)
    text = re.sub(r'&gt;',   '>',  text)
    text = re.sub(r'&#039;', "'",  text)
    text = re.sub(r'<[^>]+>', '',  text)
    return text.strip()

def hi_res(url):
    if not url: return ''
    return url.replace('50x50','500x500').replace('150x150','500x500')

def get_artists(song):
    more_info  = song.get('more_info', {})
    artist_map = more_info.get('artistMap', {})
    primary    = artist_map.get('primary_artists', [])
    if primary:
        return ', '.join(a['name'] for a in primary if a.get('name'))
    subtitle = clean(song.get('subtitle', ''))
    return subtitle.split(' - ')[0].strip() if ' - ' in subtitle else subtitle

def get_song_from_raw(raw):
    if isinstance(raw, dict):
        songs = raw.get('songs', [])
        if songs and isinstance(songs, list):
            return songs[0]
    return None

def fetch_saavn_song(song_id):
    r = requests.get(SAAVN, params={
        '__call': 'song.getDetails', 'pids': song_id,
        '_format': 'json', '_marker': '0',
        'api_version': '4', 'ctx': 'wap6dot0',
    }, headers=HEADERS, timeout=10)
    r.raise_for_status()
    song = get_song_from_raw(r.json())
    if not song:
        raise Exception("Song not found")
    return song

def get_saavn_stream_url(song):
    encrypted = song.get('more_info', {}).get('encrypted_media_url', '')
    if not encrypted:
        raise Exception("No encrypted_media_url")
    return decrypt_url(encrypted)

# ── YouTube fallback (yt-dlp) ─────────────────────────────────────────
try:
    import yt_dlp
    YT_AVAILABLE = True
    print("✅ yt-dlp available for YouTube fallback")
except ImportError:
    YT_AVAILABLE = False
    print("⚠️  yt-dlp not installed — YouTube fallback disabled")

# Cookie setup for YouTube
COOKIE_PATH = None
def setup_cookies():
    global COOKIE_PATH
    content = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if content:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.write(content); tmp.flush(); tmp.close()
        COOKIE_PATH = tmp.name
        print(f"🍪 YouTube cookies loaded")
    else:
        local = os.path.join(os.path.dirname(__file__), 'cookies.txt')
        if os.path.exists(local):
            COOKIE_PATH = local

if YT_AVAILABLE:
    setup_cookies()

YT_STRATEGIES = [
    {'extractor_args': {'youtube': {'player_client': ['android_vr']}}},
    {'extractor_args': {'youtube': {'player_client': ['android']}}},
    {'extractor_args': {'youtube': {'player_client': ['tv_embedded']}}},
    {},
]

def get_yt_audio(vid_id):
    if not YT_AVAILABLE:
        raise Exception("yt-dlp not available")
    base = {
        'quiet': True, 'no_warnings': True,
        'format': 'bestaudio[vcodec=none][acodec!=none]/bestaudio',
    }
    if COOKIE_PATH:
        base['cookiefile'] = COOKIE_PATH
    for extra in YT_STRATEGIES:
        try:
            opts = {**base, **extra}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info  = ydl.extract_info(f"https://www.youtube.com/watch?v={vid_id}", download=False)
                fmts  = info.get('formats', [])
                audio = [f for f in fmts if f.get('vcodec') in ('none',None,'') and f.get('acodec') not in ('none',None,'') and f.get('url')]
                if not audio:
                    audio = [f for f in fmts if f.get('url')]
                if not audio: continue
                best = sorted(audio, key=lambda f: f.get('abr') or 0, reverse=True)[0]
                return best['url'], info
        except Exception as e:
            print(f"YT strategy failed: {e}")
    raise Exception("All YouTube strategies failed")

def search_youtube(q, limit=10):
    if not YT_AVAILABLE:
        return []
    try:
        opts = {
            'quiet': True, 'no_warnings': True,
            'extract_flat': True, 'playlistend': limit,
        }
        if COOKIE_PATH:
            opts['cookiefile'] = COOKIE_PATH
        with yt_dlp.YoutubeDL(opts) as ydl:
            info    = ydl.extract_info(f"ytsearch{limit}:{q} music", download=False)
            results = []
            for e in info.get('entries', []):
                dur = e.get('duration') or 0
                if dur > 600: continue
                vid_id = e.get('id', '')
                results.append({
                    'id':       f"yt_{vid_id}",
                    'title':    e.get('title', ''),
                    'channel':  e.get('uploader', ''),
                    'duration': dur,
                    'thumb':    f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
                    'source':   'youtube',
                })
            return results
    except Exception as e:
        print(f"YT search failed: {e}")
        return []


# ── Routes ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'yt_available': YT_AVAILABLE, 'cookies': bool(COOKIE_PATH)})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'yt_available': YT_AVAILABLE, 'cookies': bool(COOKIE_PATH)})


@app.route('/search')
def search():
    q      = request.args.get('q', '').strip()
    source = request.args.get('source', 'both')  # saavn | youtube | both
    if not q:
        return jsonify([])
    try:
        songs = []

        # JioSaavn results
        if source in ('saavn', 'both'):
            try:
                r = requests.get(SAAVN, params={
                    '__call': 'search.getResults', 'q': q,
                    '_format': 'json', '_marker': '0',
                    'api_version': '4', 'ctx': 'wap6dot0',
                    'n': '20', 'p': '1',
                }, headers=HEADERS, timeout=10)
                r.raise_for_status()
                for s in r.json().get('results', []):
                    if s.get('type') != 'song' or not s.get('id'): continue
                    duration = 0
                    try: duration = int(s.get('more_info', {}).get('duration', 0))
                    except: pass
                    songs.append({
                        'id':       s['id'],
                        'title':    clean(s.get('title', '')),
                        'channel':  get_artists(s),
                        'duration': duration,
                        'thumb':    hi_res(s.get('image', '')),
                        'source':   'saavn',
                    })
            except Exception as e:
                print(f"Saavn search error: {e}")

        # YouTube fallback — fill remaining slots or if explicitly requested
        if source == 'youtube' or (source == 'both' and len(songs) < 5):
            yt_songs = search_youtube(q, limit=15)
            songs.extend(yt_songs)

        return jsonify(songs[:20])

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/trending')
def trending():
    """Get currently trending songs from JioSaavn."""
    try:
        lang = request.args.get('lang', 'hindi')
        r = requests.get(SAAVN, params={
            '__call':        'content.getTrending',
            'entity_type':   'song',
            'entity_language': lang,
            '_format':       'json',
            '_marker':       '0',
            'api_version':   '4',
            'ctx':           'wap6dot0',
            'n':             '20',
        }, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data  = r.json()
        items = data if isinstance(data, list) else data.get('results', data.get('songs', []))
        songs = []
        for s in items:
            if not isinstance(s, dict): continue
            sid = s.get('id', '')
            if not sid: continue
            duration = 0
            try: duration = int(s.get('more_info', {}).get('duration', 0))
            except: pass
            songs.append({
                'id':       sid,
                'title':    clean(s.get('title', '') or s.get('song', '')),
                'channel':  get_artists(s),
                'duration': duration,
                'thumb':    hi_res(s.get('image', '')),
                'source':   'saavn',
            })
        return jsonify(songs[:20])
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/stream')
def stream():
    song_id = request.args.get('id', '').strip()
    if not song_id:
        return jsonify({'error': 'no id'}), 400
    try:
        # YouTube song
        if song_id.startswith('yt_'):
            vid_id = song_id[3:]
            _, info = get_yt_audio(vid_id)
            return jsonify({
                'title':    info.get('title', ''),
                'channel':  info.get('uploader', ''),
                'thumb':    info.get('thumbnail', ''),
                'duration': info.get('duration', 0),
                'url':      f"/proxy?id={song_id}",
                'source':   'youtube',
            })

        # JioSaavn song
        song = fetch_saavn_song(song_id)
        get_saavn_stream_url(song)  # validate
        duration = 0
        try: duration = int(song.get('more_info', {}).get('duration', 0))
        except: pass
        return jsonify({
            'title':    clean(song.get('title', '')),
            'channel':  get_artists(song),
            'thumb':    hi_res(song.get('image', '')),
            'duration': duration,
            'url':      f"/proxy?id={song_id}",
            'source':   'saavn',
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/proxy')
def proxy():
    song_id = request.args.get('id', '').strip()
    if not song_id:
        return jsonify({'error': 'no id'}), 400
    try:
        if song_id.startswith('yt_'):
            vid_id     = song_id[3:]
            stream_url, _ = get_yt_audio(vid_id)
            content_type  = 'audio/webm'
        else:
            song       = fetch_saavn_song(song_id)
            stream_url = get_saavn_stream_url(song)
            content_type = 'audio/mpeg'

        print(f"🔊 Proxying [{song_id[:8]}]: {stream_url[:70]}...")

        req_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept':     '*/*',
            'Referer':    'https://www.jiosaavn.com/',
        }
        if request.headers.get('Range'):
            req_headers['Range'] = request.headers['Range']

        sr = requests.get(stream_url, headers=req_headers, stream=True, timeout=30)

        resp_headers = {
            'Content-Type':                content_type,
            'Accept-Ranges':               'bytes',
            'Cache-Control':               'no-cache',
            'Access-Control-Allow-Origin': '*',
        }
        if 'Content-Length' in sr.headers: resp_headers['Content-Length'] = sr.headers['Content-Length']
        if 'Content-Range'  in sr.headers: resp_headers['Content-Range']  = sr.headers['Content-Range']

        return Response(
            stream_with_context(sr.iter_content(chunk_size=16384)),
            status=sr.status_code, headers=resp_headers
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/lyrics')
def lyrics():
    """
    Fetch lyrics from LRCLib (free, no key needed).
    Pass title and artist as query params.
    """
    title  = request.args.get('title',  '').strip()
    artist = request.args.get('artist', '').strip()
    if not title:
        return jsonify({'error': 'title required'}), 400
    try:
        # Try exact match first
        r = requests.get('https://lrclib.net/api/search', params={
            'track_name':   title,
            'artist_name':  artist,
        }, headers={'User-Agent': 'FreeBeat/1.0'}, timeout=8)
        r.raise_for_status()
        results = r.json()

        if not results:
            return jsonify({'lyrics': None, 'synced': False})

        best = results[0]
        # Prefer synced lyrics (LRC format with timestamps)
        synced = best.get('syncedLyrics', '')
        plain  = best.get('plainLyrics',  '')

        if synced:
            return jsonify({'lyrics': synced, 'synced': True,  'source': 'lrclib'})
        elif plain:
            return jsonify({'lyrics': plain,  'synced': False, 'source': 'lrclib'})
        else:
            return jsonify({'lyrics': None,   'synced': False})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"🎵 FreeBeat on port {port} | YT={YT_AVAILABLE} | Cookies={bool(COOKIE_PATH)}")
    app.run(host='0.0.0.0', port=port, debug=False)