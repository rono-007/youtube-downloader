# Import necessary libraries
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import threading
import queue
import time
import re
import requests # Added for Gemini API calls
from flask import send_from_directory
app = Flask(__name__)
@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')
# Initialize the Flask application
# Enable CORS for all routes, allowing your frontend to communicate with this backend
CORS(app)

# --- Configuration ---
# IMPORTANT: Set the full path to your ffmpeg executable here.
# Example for Windows: r'C:\path\to\ffmpeg\bin\ffmpeg.exe'
# Example for Linux/macOS: '/usr/local/bin/ffmpeg' or '/opt/homebrew/bin/ffmpeg'
# If ffmpeg is already in your system's PATH, you can leave this as None or an empty string.
FFMPEG_PATH = os.getenv('FFMPEG_PATH', '/usr/bin/ffmpeg') # <--- Your FFMPEG PATH

# IMPORTANT: Gemini API Key. Leave as empty string for Canvas environment to inject.
GEMINI_API_KEY = ""


# Directory to save downloaded files. Create it if it doesn't exist.
DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# A simple queue to manage download tasks.
# This helps prevent the server from hanging on long downloads
# and allows for more robust error handling.
download_queue = queue.Queue()
# A dictionary to store download statuses, keyed by a unique task ID
download_statuses = {}

# --- Helper Functions for yt-dlp ---

def sanitize_filename(filename):
    """
    Sanitizes a string to be a valid filename.
    Removes invalid characters and replaces spaces with underscores.
    """
    # Remove characters that are invalid in most file systems
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Replace spaces with underscores
    filename = filename.replace(' ', '_')
    # Limit filename length to avoid issues on some file systems
    return filename[:200]

def get_available_formats_info(info):
    """
    Extracts available video qualities (MP4) and audio qualities (MP3)
    from yt-dlp info dictionary.
    """
    if not info:
        return {'video': [], 'audio': []}

    video_formats = []
    audio_formats = []

    # Map common video heights to quality strings
    video_quality_map = {
        360: '360p', 480: '480p', 720: '720p', 1080: '1080p',
        1440: '2K', 2160: '4K'
    }

    # Extract video formats
    for fmt in info.get('formats', []):
        # Check for video formats with 'mp4' extension and a video codec
        if fmt.get('vcodec') != 'none' and fmt.get('ext') == 'mp4' and fmt.get('height'):
            height = fmt['height']
            if height in video_quality_map:
                # Add only unique qualities to avoid duplicates
                if video_quality_map[height] not in [f['quality'] for f in video_formats]:
                    video_formats.append({
                        'quality': video_quality_map[height],
                        'format_id': fmt['format_id'],
                        'filesize': fmt.get('filesize', 0),
                        'fps': fmt.get('fps', 0)
                    })

    # Sort video formats by quality (e.g., 360p, 480p, ...)
    quality_order = {'360p': 1, '480p': 2, '720p': 3, '1080p': 4, '2K': 5, '4K': 6}
    video_formats.sort(key=lambda x: quality_order.get(x['quality'], 0))

    # For audio, we'll offer fixed bitrates as per frontend request,
    # as yt-dlp often gives various bitrates and converting to specific ones is common.
    # In a more advanced setup, you'd parse available audio qualities.
    audio_formats = ['192kbps', '256kbps', '320kbps'] # These are target bitrates for FFmpeg

    return {'video': video_formats, 'audio': audio_formats}


def download_worker():
    """
    Worker function to process download tasks from the queue.
    Runs in a separate thread.
    """
    while True:
        task_id, url, format_option, quality = download_queue.get()
        download_statuses[task_id] = {'status': 'processing', 'progress': 0, 'file_path': None, 'error': None}
        print(f"Starting download for task {task_id}: {url} ({format_option}, {quality})")

        try:
            # First, get video info to determine title for filename
            ydl_info_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
            if FFMPEG_PATH: # Pass ffmpeg location for info extraction if specified
                ydl_info_opts['ffmpeg_location'] = FFMPEG_PATH
            with yt_dlp.YoutubeDL(ydl_info_opts) as ydl_info:
                info_dict = ydl_info.extract_info(url, download=False)
                if not info_dict:
                    raise Exception("Could not retrieve video information.")
                video_title = info_dict.get('title', 'downloaded_file')
                sanitized_title = sanitize_filename(video_title)

            final_ext = 'mp3' if format_option == 'mp3' else 'mp4'
            output_template = os.path.join(DOWNLOAD_FOLDER, f"{sanitized_title}.%(ext)s")

            ydl_opts = {
                'outtmpl': output_template,
                'noplaylist': True, # Do not download playlists
                'progress_hooks': [lambda d: progress_hook(d, task_id)], # Custom progress hook
                'postprocessors': [],
                'quiet': True, # Suppress console output from yt-dlp
                'no_warnings': True, # Suppress warnings
            }

            # Add ffmpeg location if specified
            if FFMPEG_PATH:
                ydl_opts['ffmpeg_location'] = FFMPEG_PATH

            if format_option == 'mp3':
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'].append({
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': quality.replace('kbps', ''), # e.g., '192' from '192kbps'
                })
                # Ensure the output template uses .mp3 extension for MP3
                ydl_opts['outtmpl'] = os.path.join(DOWNLOAD_FOLDER, f"{sanitized_title}.mp3")
            elif format_option == 'mp4':
                # Explicitly request best video and best audio formats
                # Then use FFmpegMerger to combine them and ensure AAC audio.
                if quality == '360p':
                    ydl_opts['format'] = 'bestvideo[height<=360]+bestaudio'
                elif quality == '480p':
                    ydl_opts['format'] = 'bestvideo[height<=480]+bestaudio'
                elif quality == '720p':
                    ydl_opts['format'] = 'bestvideo[height<=720]+bestaudio'
                elif quality == '1080p':
                    ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio'
                elif quality == '2K': # Corresponds to 1440p
                    ydl_opts['format'] = 'bestvideo[height<=1440]+bestaudio'
                elif quality == '4K': # Corresponds to 2160p
                    ydl_opts['format'] = 'bestvideo[height<=2160]+bestaudio'
                else: # Fallback for any unhandled quality, or 'best'
                    ydl_opts['format'] = 'bestvideo+bestaudio'
                
                # Use FFmpegMerger to combine video and audio, and re-encode audio to aac
                ydl_opts['postprocessors'].append({
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4'
                    })

                 # Ensure the output template uses .mp4 extension
                ydl_opts['outtmpl'] = os.path.join(DOWNLOAD_FOLDER, f"{sanitized_title}.%(ext)s")

                found_file = None
                expected_ext = 'mp4' if format_option == 'mp4' else 'mp3'

# Check for file with correct prefix and extension
                for fname in os.listdir(DOWNLOAD_FOLDER):
                    if fname.startswith(sanitized_title) and fname.endswith(f".{expected_ext}"):
                        found_file = os.path.join(DOWNLOAD_FOLDER, fname)
                        break

# Update download status
                if found_file and os.path.exists(found_file):
                    download_statuses[task_id]['status'] = 'completed'
                    download_statuses[task_id]['progress'] = 100
                    download_statuses[task_id]['file_path'] = found_file
                    print(f"Download completed for task {task_id}: {found_file}")
                else:
                    download_statuses[task_id]['status'] = 'failed'
                    download_statuses[task_id]['error'] = 'File not found after download. Check yt-dlp output for exact filename.'
                    print(f"Download failed for task {task_id}: File not found.")


            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

                # Check if the file exists after download
                found_file = None
                # yt-dlp might add format_id to filename, so we look for files starting with sanitized title
                for fname in os.listdir(DOWNLOAD_FOLDER):
                    if fname.startswith(sanitized_title) and fname.endswith(f".{final_ext}"):
                        found_file = os.path.join(DOWNLOAD_FOLDER, fname)
                        break

                if found_file and os.path.exists(found_file):
                    download_statuses[task_id]['status'] = 'completed'
                    download_statuses[task_id]['progress'] = 100
                    download_statuses[task_id]['file_path'] = found_file
                    print(f"Download completed for task {task_id}: {found_file}")
                else:
                    download_statuses[task_id]['status'] = 'failed'
                    download_statuses[task_id]['error'] = 'File not found after download. Check yt-dlp output for exact filename.'
                    print(f"Download failed for task {task_id}: File not found.")

        except yt_dlp.DownloadError as e:
            download_statuses[task_id]['status'] = 'failed'
            download_statuses[task_id]['error'] = str(e)
            print(f"Download failed for task {task_id} with yt-dlp error: {e}")
        except Exception as e:
            download_statuses[task_id]['status'] = 'failed'
            download_statuses[task_id]['error'] = f"An unexpected error occurred: {str(e)}"
            print(f"Download failed for task {task_id} with unexpected error: {e}")
        finally:
            download_queue.task_done() # Mark the task as done in the queue

def progress_hook(d, task_id):
    """
    Custom progress hook for yt-dlp to update download status.
    """
    if d['status'] == 'downloading':
        if '_percent_str' in d:
            try:
                progress = float(d['_percent_str'].replace('%', '').strip())
                download_statuses[task_id]['progress'] = progress
            except ValueError:
                pass # Ignore if percentage string is not a valid float
    elif d['status'] == 'finished':
        download_statuses[task_id]['progress'] = 100
        download_statuses[task_id]['status'] = 'completed'
        if 'filename' in d:
            download_statuses[task_id]['file_path'] = d['filename']
        print(f"Task {task_id} finished downloading.")

# Start the download worker thread
threading.Thread(target=download_worker, daemon=True).start()

# --- API Endpoints ---

@app.route('/get_video_info', methods=['POST'])
def get_video_info():
    """
    API endpoint to get video title, thumbnail, and available formats from a YouTube URL.
    """
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    try:
        # Use yt-dlp to extract info without downloading
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'force_generic_extractor': True, # Helps with some URLs
            'no_warnings': True,
        }
        # Add ffmpeg location if specified for info extraction (though usually not needed here)
        if FFMPEG_PATH:
            ydl_opts['ffmpeg_location'] = FFMPEG_PATH

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'No Title Found')
            thumbnail = info.get('thumbnail', 'https://placehold.co/480x270/e0e0e0/555555?text=No+Thumbnail')

            # Get available formats using the new helper function
            available_formats = get_available_formats_info(info)

            return jsonify({
                'title': title,
                'thumbnail': thumbnail,
                'available_formats': available_formats
            })
    except yt_dlp.DownloadError as e:
        return jsonify({'error': f'Could not get video info: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500

@app.route('/download', methods=['POST'])
def download_video():
    """
    API endpoint to initiate video/audio download.
    Returns a task ID for status polling.
    """
    data = request.json
    url = data.get('url')
    format_option = data.get('format') # 'mp4' or 'mp3'
    quality = data.get('quality') # e.g., '720p', '192kbps'

    if not all([url, format_option, quality]):
        return jsonify({'error': 'Missing URL, format, or quality'}), 400

    # Generate a unique task ID
    task_id = os.urandom(16).hex()
    download_statuses[task_id] = {'status': 'queued', 'progress': 0, 'file_path': None, 'error': None}
    download_queue.put((task_id, url, format_option, quality))
    print(f"Download task {task_id} queued for {url}")
    return jsonify({'message': 'Download started', 'taskId': task_id})

@app.route('/download_status/<task_id>', methods=['GET'])
def get_download_status(task_id):
    """
    API endpoint to check the status of a download task.
    """
    status = download_statuses.get(task_id)
    if not status:
        return jsonify({'error': 'Task ID not found'}), 404
    return jsonify(status)

@app.route('/get_file/<task_id>', methods=['GET'])
def get_downloaded_file(task_id):
    """
    API endpoint to serve the downloaded file once it's complete.
    """
    status = download_statuses.get(task_id)
    if not status or status['status'] != 'completed' or not status['file_path']:
        return jsonify({'error': 'File not ready or task not found'}), 404

    file_path = status['file_path']
    if os.path.exists(file_path):
        # Determine mimetype based on file extension
        # Use os.path.splitext to get the actual extension from the downloaded file
        _, ext = os.path.splitext(file_path)
        mimetype = 'video/mp4' if ext.lower() == '.mp4' else 'audio/mpeg'
        return send_file(file_path, as_attachment=True, mimetype=mimetype)
    else:
        return jsonify({'error': 'File not found on server'}), 404


# Run the Flask app
if __name__ == '__main__':
    # Run on a different port than your frontend (e.g., 5000)
    app.run(host='0.0.0.0', port=5000)
