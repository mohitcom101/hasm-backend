import sys
import time
import requests
import urllib.parse

# Attempt importing the extraction logic directly for fallback
try:
    sys.path.append('.')
    from main import extract_audio_stream
    import_success = True
except ImportError:
    import_success = False

def check_stream_url(stream_url):
    print("Checking stream URL headers...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Range': 'bytes=0-1024'
    }
    
    try:
        start_time = time.time()
        res = requests.get(stream_url, headers=headers, stream=True, timeout=5)
        duration = time.time() - start_time
        
        print(f"Stream response status code: {res.status_code}")
        print(f"Stream headers retrieval time: {duration:.2f} seconds")
        print(f"Content-Type: {res.headers.get('Content-Type')}")
        print(f"Content-Length: {res.headers.get('Content-Length')} bytes (partial request)")
        
        accept_ranges = res.headers.get('Accept-Ranges')
        content_range = res.headers.get('Content-Range')
        
        print(f"Accept-Ranges header: {accept_ranges}")
        print(f"Content-Range header: {content_range}")
        
        range_supported = (res.status_code == 206) or (accept_ranges == 'bytes') or (content_range is not None)
        
        if range_supported:
            print("? SUCCESS: Stream supports HTTP Range Requests (essential for near-zero buffering and fast seeking)!")
        else:
            print("?? WARNING: Stream might not support Range Requests. Buffering may be slower.")
            
        return range_supported
    except Exception as e:
        print(f"? ERROR: Failed to connect to stream URL: {e}")
        return False

def run_test():
    query = "Talwiinder - Khayaal"
    print(f"Testing audio extraction for heavy-bass track: '{query}'")
    
    backend_url = f"http://127.0.0.1:5000/stream?query={urllib.parse.quote(query)}"
    print(f"Attempting to query local FastAPI server at {backend_url}...")
    
    local_server_running = False
    start_time = time.time()
    try:
        res = requests.get(backend_url, timeout=5)
        duration = time.time() - start_time
        if res.status_code == 200:
            print(f"? Local server responded in {duration:.2f} seconds.")
            data = res.json()
            local_server_running = True
        else:
            print(f"Local server returned status code: {res.status_code}")
    except requests.exceptions.RequestException:
        print("Local server is not running on port 5000. Falling back to direct in-process extraction...")
        
    if not local_server_running:
        if not import_success:
            print("? ERROR: Cannot run direct extraction. Make sure to run the script inside backend folder.")
            sys.exit(1)
        start_time = time.time()
        try:
            search_target = f"ytsearch1:{query}"
            data = extract_audio_stream(search_target)
            duration = time.time() - start_time
            print(f"? In-process extraction completed in {duration:.2f} seconds.")
        except Exception as e:
            print(f"? ERROR: In-process extraction failed: {e}")
            sys.exit(1)
            
    print("\n--- Metadata Result ---")
    print(f"ID: {data.get('id')}")
    print(f"Title: {data.get('title')}")
    print(f"Artist: {data.get('artist')}")
    print(f"Thumbnail URL: {data.get('thumbnail')}")
    print(f"Duration: {data.get('duration')} seconds")
    print(f"Stream URL: {data.get('stream_url')[:100]}...")
    
    if duration < 2.0:
        print(f"? SUCCESS: URL extraction took {duration:.2f} seconds (under 2-second target)!")
    else:
        print(f"?? WARNING: URL extraction took {duration:.2f} seconds (over 2-second target).")
        
    assert data.get('stream_url'), "Stream URL must be present"
    assert data.get('title'), "Title must be present"
    assert data.get('artist'), "Artist must be present"
    
    print("\n--- Stream URL Performance & Buffering Check ---")
    check_stream_url(data.get('stream_url'))

if __name__ == "__main__":
    run_test()
