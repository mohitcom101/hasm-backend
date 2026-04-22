from flask import Flask, request, jsonify
from flask_cors import CORS
from ytmusicapi import YTMusic
import yt_dlp
import requests
import os
import time

# On Render: write cookies from environment variable to disk
_cookies_env = os.environ.get('COOKIES_CONTENT')
if _cookies_env:
    with open('cookies.txt', 'w') as _f:
        _f.write(_cookies_env)

app = Flask(__name__)
CORS(app)

ytmusic = YTMusic()

@app.route('/search', methods=['GET'])
def search():
    query = request.args.get('q', '')
    if not query:
        return jsonify({'error': 'Query is required'}), 400
    try:
        results = ytmusic.search(query, filter='songs')
        songs = []
        for res in results:
            if res.get('resultType') in ['song', 'video']:
                # The result format from ytmusicapi for songs usually has 'videoId'
                video_id = res.get('videoId')
                if not video_id:
                    continue
                artists = ', '.join([a['name'] for a in res.get('artists', []) if 'name' in a])
                album = res.get('album', {}).get('name') if isinstance(res.get('album'), dict) else ''
                thumbnails = res.get('thumbnails', [])
                thumbnail = thumbnails[-1].get('url') if thumbnails else ''
                
                songs.append({
                    'id': video_id,
                    'title': res.get('title'),
                    'artist': artists,
                    'album': album,
                    'thumbnail': thumbnail,
                    'duration': res.get('duration')
                })
        return jsonify(songs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

import time

# In-memory cache to reduce YouTube hits
# Format: {video_id: {'url': stream_url, 'expires': timestamp}}
stream_cache = {}
CACHE_DURATION = 3600 * 2  # 2 hours

@app.route('/stream/<video_id>', methods=['GET'])
def stream(video_id):
    # Check cache first
    global stream_cache
    if video_id in stream_cache:
        cached = stream_cache[video_id]
        if time.time() < cached['expires']:
            return jsonify(cached['data'])
        else:
            del stream_cache[video_id]

    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        # Robust options to avoid 429
        # Linked cookies.txt for authenticated, bot-resistant requests
        ydl_opts = {
            'format': 'bestaudio',
            'quiet': True,
            'no_playlist': True,
            'extract_flat': False,
            'cookiefile': 'cookies.txt', # Using your exported cookies
            'extractor_args': {
                'youtube': {
                    'player_client': ['web_music', 'android_vr', 'ios', 'tv'],
                }
            },
            # Using a more generic UA to avoid common bot detection
            'userAgent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Prefer m4a for best compatibility with expo-av
            formats = info.get('formats', [])
            m4a_formats = [f for f in formats if f.get('ext') == 'm4a' and f.get('acodec') != 'none']
            
            if m4a_formats:
                stream_url = m4a_formats[-1].get('url')
            else:
                stream_url = info.get('url')

            result = {
                'stream_url': stream_url,
                'title': info.get('title'),
                'artist': info.get('uploader'),
                'thumbnail': info.get('thumbnail')
            }

            # Update cache
            stream_cache[video_id] = {
                'data': result,
                'expires': time.time() + CACHE_DURATION
            }
            
            return jsonify(result)
            
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg:
            return jsonify({'error': 'Too many requests. Please try again later or add cookies to your backend.'}), 429
        return jsonify({'error': error_msg}), 500

@app.route('/lyrics', methods=['GET'])
def lyrics():
    title = request.args.get('title', '')
    artist = request.args.get('artist', '')
    if not title or not artist:
        return jsonify({'error': 'Title and artist are required'}), 400
        
    try:
        url = f"https://lrclib.net/api/get"
        response = requests.get(url, params={'track_name': title, 'artist_name': artist})
        if response.status_code == 200:
            data = response.json()
            return jsonify({
                'synced_lyrics': data.get('syncedLyrics'),
                'plain_lyrics': data.get('plainLyrics')
            })
        else:
            return jsonify({'error': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/suggestions', methods=['GET'])
def suggestions():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    try:
        suggestions = ytmusic.get_search_suggestions(query)
        # return list format, it may be list of str or dict, just return as is
        return jsonify(suggestions)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/home', methods=['GET'])
def home():
    try:
        results = ytmusic.search('top hits latest', filter='songs', limit=15)
        songs = []
        for res in results:
            if res.get('resultType') in ['song', 'video']:
                video_id = res.get('videoId')
                if not video_id:
                    continue
                artists = ', '.join([a['name'] for a in res.get('artists', []) if 'name' in a])
                album = res.get('album', {}).get('name') if isinstance(res.get('album'), dict) else ''
                thumbnails = res.get('thumbnails', [])
                thumbnail = thumbnails[-1].get('url') if thumbnails else ''
                
                songs.append({
                    'id': video_id,
                    'title': res.get('title'),
                    'artist': artists,
                    'album': album,
                    'thumbnail': thumbnail,
                    'duration': res.get('duration')
                })
        # Try fetching charts too, but search is more reliable for format
        return jsonify(songs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/upnext', methods=['GET'])
def upnext():
    video_id = request.args.get('video_id', '')
    if not video_id:
        return jsonify({'error': 'video_id is required'}), 400
    try:
        watch_playlist = ytmusic.get_watch_playlist(videoId=video_id)
        tracks = watch_playlist.get('tracks', [])
        songs = []
        # skip the first one since it's the current song usually
        for res in tracks[1:]:
            vid = res.get('videoId')
            if not vid:
                continue
            artists = ', '.join([a['name'] for a in res.get('artists', []) if 'name' in a])
            album = res.get('album', {}).get('name') if isinstance(res.get('album'), dict) else ''
            thumbnails = res.get('thumbnails', [])
            thumbnail = thumbnails[-1].get('url') if thumbnails else ''
            # format duration
            dur_seconds = res.get('lengthMs', 0) // 1000 if res.get('lengthMs') else res.get('duration', '0:00')
            if isinstance(dur_seconds, int):
                m = dur_seconds // 60
                s = dur_seconds % 60
                duration_str = f"{m}:{s:02d}"
            else:
                duration_str = dur_seconds

            songs.append({
                'id': vid,
                'title': res.get('title'),
                'artist': artists,
                'album': album,
                'thumbnail': thumbnail,
                'duration': duration_str
            })
        return jsonify(songs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
