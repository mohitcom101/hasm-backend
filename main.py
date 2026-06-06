import os
import time
import logging
import requests
import yt_dlp
import httpx
from fastapi import FastAPI, Query, HTTPException, Path, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from ytmusicapi import YTMusic
from typing import List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("music-player-backend")

app = FastAPI(
    title="Online Music Player Backend",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_cookies_env = os.environ.get('COOKIES_CONTENT')
if _cookies_env:
    with open('cookies.txt', 'w') as _f:
        _f.write(_cookies_env)
    logger.info("Created cookies.txt from COOKIES_CONTENT.")

try:
    ytmusic = YTMusic()
    logger.info("YTMusic Client initialized.")
except Exception as e:
    logger.error(f"Failed to initialize YTMusic: {e}")
    ytmusic = None

stream_cache = {}
CACHE_DURATION = 3600 * 2

def get_cookies_path() -> Optional[str]:
    # Prioritize root cookies.txt (usually fresher) over backend/cookies.txt
    paths = ['cookies.txt', 'backend/cookies.txt']
    for p in paths:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None

def extract_audio_stream(url_or_search: str) -> dict:
    cookie_file = get_cookies_path()
    ydl_opts = {
        'quiet': True,
        'no_playlist': True,
        'extract_flat': False,
        'skip_download': True,
        'check_formats': False,
        'youtube_include_dash_manifest': False,
        'youtube_include_hls_manifest': False,
        'remote_components': {'ejs:github'},
        'js_runtimes': {'node': {}},
        'extractor_args': {
            'youtube': {
                'player_client': ['default', '-android_sdkless'],
            }
        },
        'userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url_or_search, download=False)
        if 'entries' in info:
            if not info['entries']:
                raise HTTPException(status_code=404, detail="No YouTube results found.")
            entry = info['entries'][0]
        else:
            entry = info

        formats = entry.get('formats', [])
        
        # Filter for audio-only formats (has audio codec, no video codec)
        audio_formats = [
            f for f in formats 
            if f.get('url') and 
            f.get('acodec') not in [None, 'none'] and 
            f.get('vcodec') in [None, 'none']
        ]
        
        # Fallback to any formats with audio
        if not audio_formats:
            audio_formats = [
                f for f in formats 
                if f.get('url') and 
                f.get('acodec') not in [None, 'none']
            ]

        # Prefer m4a formats
        m4a_formats = [f for f in audio_formats if f.get('ext') == 'm4a']
        
        stream_url = None
        if m4a_formats:
            m4a_formats.sort(key=lambda f: f.get('abr', 0), reverse=True)
            stream_url = m4a_formats[0].get('url')
        
        if not stream_url and audio_formats:
            audio_formats.sort(key=lambda f: f.get('abr', 0), reverse=True)
            stream_url = audio_formats[0].get('url')
        
        if not stream_url:
            stream_url = entry.get('url')

        if not stream_url:
            raise HTTPException(status_code=500, detail="Failed to retrieve stream URL.")

        artist = entry.get('artist') or entry.get('creator') or entry.get('uploader') or "Unknown Artist"
        if artist.endswith(" - Topic"):
            artist = artist[:-8]

        return {
            'id': entry.get('id'),
            'stream_url': stream_url,
            'title': entry.get('title'),
            'artist': artist,
            'thumbnail': entry.get('thumbnail'),
            'duration': entry.get('duration')
        }

real_url_cache = {}

def prefetch_songs_task(video_ids: List[str]):
    global stream_cache, real_url_cache
    for video_id in video_ids:
        # Check if already cached and valid
        if video_id in stream_cache:
            cached = stream_cache[video_id]
            if time.time() < cached['expires']:
                continue
        try:
            logger.info(f"[Background Prefetch] Resolving stream for ID: {video_id}")
            url = f"https://www.youtube.com/watch?v={video_id}"
            result = extract_audio_stream(url)
            stream_cache[video_id] = {
                'data': result,
                'expires': time.time() + CACHE_DURATION
            }
            real_url_cache[video_id] = result['stream_url']
            logger.info(f"[Background Prefetch] Successfully resolved stream for ID: {video_id}")
        except Exception as e:
            logger.warning(f"[Background Prefetch] Failed resolving stream for ID {video_id}: {e}")

def get_proxy_stream_url(video_id: str, real_url: str, request: Request) -> str:
    global real_url_cache
    real_url_cache[video_id] = real_url
    base_url = str(request.base_url).rstrip('/')
    return f"{base_url}/stream-media/{video_id}"

@app.get("/stream")
def stream_by_query(request: Request, query: str = Query(..., description="Query or direct URL")):
    global stream_cache
    if query in stream_cache:
        cached = stream_cache[query]
        if time.time() < cached['expires']:
            result = cached['data'].copy()
            result['stream_url'] = get_proxy_stream_url(result['id'], result['stream_url'], request)
            return result
        else:
            del stream_cache[query]

    try:
        search_target = query if (query.startswith("http://") or query.startswith("https://")) else f"ytsearch1:{query}"
        result = extract_audio_stream(search_target)
        stream_cache[query] = {
            'data': result.copy(),
            'expires': time.time() + CACHE_DURATION
        }
        result['stream_url'] = get_proxy_stream_url(result['id'], result['stream_url'], request)
        return result
    except yt_dlp.utils.DownloadError as de:
        error_msg = str(de)
        if "429" in error_msg:
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Provide fresh cookies.")
        raise HTTPException(status_code=500, detail=error_msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stream/{video_id}")
def stream_by_video_id(video_id: str = Path(..., description="Video ID"), request: Request = None):
    global stream_cache
    if video_id in stream_cache:
        cached = stream_cache[video_id]
        if time.time() < cached['expires']:
            result = cached['data'].copy()
            result['stream_url'] = get_proxy_stream_url(result['id'], result['stream_url'], request)
            return result
        else:
            del stream_cache[video_id]

    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        result = extract_audio_stream(url)
        stream_cache[video_id] = {
            'data': result.copy(),
            'expires': time.time() + CACHE_DURATION
        }
        result['stream_url'] = get_proxy_stream_url(result['id'], result['stream_url'], request)
        return result
    except yt_dlp.utils.DownloadError as de:
        error_msg = str(de)
        if "429" in error_msg:
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Provide cookies.")
        raise HTTPException(status_code=500, detail=error_msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stream-media/{video_id}")
async def stream_media(video_id: str, request: Request):
    global real_url_cache
    real_url = real_url_cache.get(video_id)
    if not real_url:
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            res = extract_audio_stream(url)
            real_url = res['stream_url']
            real_url_cache[video_id] = real_url
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to resolve stream: {e}")

    headers = {}
    client_range = request.headers.get("range")
    if client_range:
        headers["Range"] = client_range
        
    headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    try:
        client = httpx.AsyncClient()
        req = client.build_request("GET", real_url, headers=headers)
        r = await client.send(req, stream=True)
        
        response_headers = {}
        for h in ["Content-Range", "Accept-Ranges", "Content-Length", "Content-Type"]:
            if r.headers.get(h):
                response_headers[h] = r.headers.get(h)

        async def bytes_generator():
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
            finally:
                await r.aclose()
                await client.aclose()

        return StreamingResponse(
            bytes_generator(),
            status_code=r.status_code,
            headers=response_headers
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Streaming proxy error: {e}")

@app.get("/lyrics")
def get_lyrics(title: str = Query(...), artist: str = Query(...)):
    clean_title = title.split(' (')[0].split(' - ')[0].strip()
    clean_artist = artist.split(',')[0].split('&')[0].split(' feat.')[0].strip()
    try:
        url = "https://lrclib.net/api/get"
        response = requests.get(url, params={'track_name': clean_title, 'artist_name': clean_artist}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return {
                'synced_lyrics': data.get('syncedLyrics'),
                'plain_lyrics': data.get('plainLyrics')
            }
        else:
            return {'synced_lyrics': None, 'plain_lyrics': "Lyrics not found."}
    except Exception:
        return {'synced_lyrics': None, 'plain_lyrics': "Lyrics unavailable."}

@app.get("/search")
def search(q: str = Query(...), background_tasks: BackgroundTasks = None):
    if not ytmusic:
        raise HTTPException(status_code=500, detail="YTMusic not initialized.")
    try:
        results = ytmusic.search(q, filter='songs')
        songs = []
        for res in results:
            if res.get('resultType') in ['song', 'video']:
                video_id = res.get('videoId')
                if not video_id:
                    continue
                artists = res.get('artists', [])
                artists_names = ', '.join([a['name'] for a in artists if 'name' in a])
                artist_id = artists[0].get('id') if artists else None
                
                album_obj = res.get('album')
                album_name = ''
                album_id = None
                if isinstance(album_obj, dict):
                    album_name = album_obj.get('name', '')
                    album_id = album_obj.get('id')
                
                thumbnails = res.get('thumbnails', [])
                thumbnail = thumbnails[-1].get('url') if thumbnails else ''
                songs.append({
                    'id': video_id,
                    'title': res.get('title'),
                    'artist': artists_names,
                    'artistId': artist_id,
                    'album': album_name,
                    'albumId': album_id,
                    'thumbnail': thumbnail,
                    'duration': res.get('duration')
                })
        if background_tasks and len(songs) > 0:
            background_tasks.add_task(prefetch_songs_task, [s['id'] for s in songs[:3]])
        return songs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/suggestions")
def suggestions(q: str = Query("")):
    if not ytmusic:
        return []
    try:
        return ytmusic.get_search_suggestions(q)
    except Exception:
        return []

@app.get("/resolve-metadata")
def resolve_metadata(video_id: str = Query(...), title: str = Query(...), artist: str = Query(...)):
    if not ytmusic:
        raise HTTPException(status_code=500, detail="YTMusic not initialized.")
    try:
        query = f"{title} {artist}"
        search_results = ytmusic.search(query, filter="songs")
        for res in search_results:
            if res.get('videoId') == video_id:
                album_obj = res.get('album')
                album_id = album_obj.get('id') if isinstance(album_obj, dict) else None
                artists = res.get('artists', [])
                artist_id = artists[0].get('id') if artists else None
                return {"albumId": album_id, "artistId": artist_id}
        
        # Fallback: match by title similarity
        for res in search_results:
            if res.get('title', '').lower() == title.lower():
                album_obj = res.get('album')
                album_id = album_obj.get('id') if isinstance(album_obj, dict) else None
                artists = res.get('artists', [])
                artist_id = artists[0].get('id') if artists else None
                return {"albumId": album_id, "artistId": artist_id}
                
        return {"albumId": None, "artistId": None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

import shutil
import subprocess

@app.get("/debug")
def debug_info():
    import io
    
    log_capture = io.StringIO()
    ydl_opts = {
        'quiet': False,
        'no_playlist': True,
        'extract_flat': False,
        'skip_download': True,
        'check_formats': False,
        'remote_components': {'ejs:github'},
        'js_runtimes': {'node': {}},
        'extractor_args': {
            'youtube': {
                'player_client': ['default', '-android_sdkless'],
            }
        },
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None
    }
    
    class MyLogger(object):
        def debug(self, msg):
            log_capture.write(msg + "\n")
        def info(self, msg):
            log_capture.write(msg + "\n")
        def warning(self, msg):
            log_capture.write("WARNING: " + msg + "\n")
        def error(self, msg):
            log_capture.write("ERROR: " + msg + "\n")

    ydl_opts['logger'] = MyLogger()
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info('https://www.youtube.com/watch?v=WC7UTfWVuAU', download=False)
            success = True
    except Exception as e:
        log_capture.write(f"EXCEPTION: {e}\n")
        success = False

    return {
        "version": "debug-v2",
        "success": success,
        "logs": log_capture.getvalue().split("\n"),
        "node_path": shutil.which("node"),
        "node_version": subprocess.getoutput("node -v") if shutil.which("node") else None,
    }


@app.get("/home")
def home(background_tasks: BackgroundTasks = None):
    if not ytmusic:
        raise HTTPException(status_code=500, detail="YTMusic not initialized.")
    try:
        results = ytmusic.search('top hits latest', filter='songs', limit=15)
        songs = []
        for res in results:
            if res.get('resultType') in ['song', 'video']:
                video_id = res.get('videoId')
                if not video_id:
                    continue
                artists = res.get('artists', [])
                artists_names = ', '.join([a['name'] for a in artists if 'name' in a])
                artist_id = artists[0].get('id') if artists else None
                
                album_obj = res.get('album')
                album_name = ''
                album_id = None
                if isinstance(album_obj, dict):
                    album_name = album_obj.get('name', '')
                    album_id = album_obj.get('id')
                
                thumbnails = res.get('thumbnails', [])
                thumbnail = thumbnails[-1].get('url') if thumbnails else ''
                songs.append({
                    'id': video_id,
                    'title': res.get('title'),
                    'artist': artists_names,
                    'artistId': artist_id,
                    'album': album_name,
                    'albumId': album_id,
                    'thumbnail': thumbnail,
                    'duration': res.get('duration')
                })
        if background_tasks and len(songs) > 0:
            background_tasks.add_task(prefetch_songs_task, [s['id'] for s in songs[:5]])
        return songs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/upnext")
def upnext(video_id: str = Query(...), background_tasks: BackgroundTasks = None):
    if not ytmusic:
        raise HTTPException(status_code=500, detail="YTMusic not initialized.")
    try:
        watch_playlist = ytmusic.get_watch_playlist(videoId=video_id)
        tracks = watch_playlist.get('tracks', [])
        songs = []
        for res in tracks[1:]:
            vid = res.get('videoId')
            if not vid:
                continue
            artists = res.get('artists', [])
            artists_names = ', '.join([a['name'] for a in artists if 'name' in a])
            artist_id = artists[0].get('id') if artists else None
            
            album_obj = res.get('album')
            album_name = ''
            album_id = None
            if isinstance(album_obj, dict):
                album_name = album_obj.get('name', '')
                album_id = album_obj.get('id')
            
            thumbnails = res.get('thumbnails', [])
            thumbnail = thumbnails[-1].get('url') if thumbnails else ''
            dur_seconds = res.get('lengthMs', 0) // 1000 if res.get('lengthMs') else res.get('duration', '0:00')
            if isinstance(dur_seconds, int):
                m = dur_seconds // 60
                s = dur_seconds % 60
                duration_str = f"{m}:{s:02d}"
            else:
                duration_str = str(dur_seconds)
            songs.append({
                'id': vid,
                'title': res.get('title'),
                'artist': artists_names,
                'artistId': artist_id,
                'album': album_name,
                'albumId': album_id,
                'thumbnail': thumbnail,
                'duration': duration_str
            })
        if background_tasks and len(songs) > 0:
            background_tasks.add_task(prefetch_songs_task, [s['id'] for s in songs[:2]])
        return songs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/album/{album_id}")
def get_album_details(album_id: str = Path(...)):
    if not ytmusic:
        raise HTTPException(status_code=500, detail="YTMusic not initialized.")
    try:
        album_data = ytmusic.get_album(album_id)
        tracks = []
        for t in album_data.get('tracks', []):
            artists = ', '.join([a['name'] for a in t.get('artists', []) if 'name' in a])
            thumbnails = t.get('thumbnails', [])
            thumbnail = thumbnails[-1].get('url') if thumbnails else (album_data.get('thumbnails', [])[-1].get('url') if album_data.get('thumbnails') else '')
            
            tracks.append({
                'id': t.get('videoId'),
                'title': t.get('title'),
                'artist': artists,
                'artistId': t.get('artists', [{}])[0].get('id') if t.get('artists') else None,
                'album': album_data.get('title'),
                'albumId': album_id,
                'thumbnail': thumbnail,
                'duration': t.get('duration')
            })
        
        return {
            'id': album_id,
            'title': album_data.get('title'),
            'artist': ', '.join([a['name'] for a in album_data.get('artists', [])]),
            'artistId': album_data.get('artists', [{}])[0].get('id') if album_data.get('artists') else None,
            'thumbnail': album_data.get('thumbnails', [])[-1].get('url') if album_data.get('thumbnails') else '',
            'year': album_data.get('year'),
            'trackCount': album_data.get('trackCount'),
            'duration': album_data.get('duration'),
            'songs': tracks
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/artist/{artist_id}")
def get_artist_details(artist_id: str = Path(...)):
    if not ytmusic:
        raise HTTPException(status_code=500, detail="YTMusic not initialized.")
    try:
        artist_data = ytmusic.get_artist(artist_id)
        
        songs = []
        songs_list = artist_data.get('songs', {}).get('results', [])
        for s in songs_list:
            artists = ', '.join([a['name'] for a in s.get('artists', []) if 'name' in a])
            thumbnails = s.get('thumbnails', [])
            thumbnail = thumbnails[-1].get('url') if thumbnails else ''
            
            songs.append({
                'id': s.get('videoId'),
                'title': s.get('title'),
                'artist': artists,
                'artistId': artist_id,
                'album': s.get('album', {}).get('name') if isinstance(s.get('album'), dict) else '',
                'albumId': s.get('album', {}).get('id') if isinstance(s.get('album'), dict) else None,
                'thumbnail': thumbnail,
                'duration': s.get('duration')
            })
            
        albums = []
        albums_list = artist_data.get('albums', {}).get('results', [])
        for a in albums_list:
            thumbnails = a.get('thumbnails', [])
            thumbnail = thumbnails[-1].get('url') if thumbnails else ''
            
            albums.append({
                'id': a.get('browseId'),
                'title': a.get('title'),
                'year': a.get('year'),
                'thumbnail': thumbnail
            })
            
        return {
            'id': artist_id,
            'name': artist_data.get('name'),
            'description': artist_data.get('description'),
            'thumbnail': artist_data.get('thumbnails', [])[-1].get('url') if artist_data.get('thumbnails') else '',
            'songs': songs,
            'albums': albums
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)
