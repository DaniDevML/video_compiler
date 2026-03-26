import os
import pickle
import re
import shutil
import subprocess
import sys

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
TOKEN_FILE = os.path.join(os.path.dirname(__file__), 'yt_token.pickle')
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), 'client_secrets.json')


def get_youtube_service():
    """Return an authenticated YouTube API service object."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                raise FileNotFoundError(
                    'YouTube credentials not set up yet. '
                    'Drag your client_secrets.json file onto the setup box on the page, '
                    'or follow the on-screen instructions to create one.'
                )
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, 'wb') as f:
            pickle.dump(creds, f)

    return build('youtube', 'v3', credentials=creds)


def upload_video(video_path: str, title: str = 'Data Archive', progress=None) -> tuple:
    """
    Upload a video to YouTube as an unlisted video.

    Returns:
        (video_id, url)
    """
    def log(msg):
        if progress:
            progress(msg)

    log('Authenticating with YouTube API...')
    youtube = get_youtube_service()

    body = {
        'snippet': {
            'title': title,
            'description': (
                'Encoded data archive. '
                'Created with vid_compiler for research purposes.'
            ),
            'categoryId': '22',
        },
        'status': {
            'privacyStatus': 'unlisted',
            'selfDeclaredMadeForKids': False,
        },
    }

    media = MediaFileUpload(
        video_path,
        mimetype='video/mp4',
        resumable=True,
        chunksize=1024 * 1024,  # 1 MB chunks
    )

    request = youtube.videos().insert(
        part='snippet,status',
        body=body,
        media_body=media,
    )

    log('Uploading to YouTube...')
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            log(f'Upload progress: {pct}%')

    video_id = response['id']
    url = f'https://www.youtube.com/watch?v={video_id}'
    log(f'Upload complete! Video ID: {video_id}')
    return video_id, url


def extract_video_id(url: str) -> str:
    """Parse a YouTube URL and return the video ID."""
    patterns = [
        r'(?:v=|youtu\.be/|/embed/|/v/)([a-zA-Z0-9_-]{11})',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    if re.fullmatch(r'[a-zA-Z0-9_-]{11}', url.strip()):
        return url.strip()
    raise ValueError(
        "That doesn't look like a valid YouTube URL. "
        'Try pasting the full link, e.g. https://www.youtube.com/watch?v=XXXXXXXXXXX'
    )


def _yt_dlp_executable() -> str:
    """Find yt-dlp, including the user-local Scripts dir pip installs to."""
    exe = shutil.which('yt-dlp')
    if exe:
        return exe
    # Fallback: Scripts dir next to the current Python interpreter
    scripts = os.path.join(os.path.dirname(sys.executable), 'Scripts', 'yt-dlp.exe')
    if os.path.exists(scripts):
        return scripts
    # Another common location for Windows Store Python
    local_scripts = os.path.join(
        os.environ.get('LOCALAPPDATA', ''),
        'Packages', 'PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0',
        'LocalCache', 'local-packages', 'Python312', 'Scripts', 'yt-dlp.exe',
    )
    if os.path.exists(local_scripts):
        return local_scripts
    raise FileNotFoundError(
        'The video downloader (yt-dlp) is not installed. '
        'Open a terminal and run:  pip install yt-dlp  — then restart the app.'
    )


def download_video(url: str, output_path: str, progress=None) -> str:
    """
    Download a YouTube video at 1080p using yt-dlp.
    Returns the actual path of the downloaded file.
    """
    def log(msg):
        if progress:
            progress(msg)

    import glob
    import tempfile

    video_id = extract_video_id(url)
    log(f'Downloading video {video_id}...')

    # Download into a dedicated temp dir so we can find the file regardless of extension
    dl_dir = tempfile.mkdtemp()
    template = os.path.join(dl_dir, 'video')

    # Video-only download at 1080p — no audio merge needed (we just need frames)
    fmt = 'bestvideo[height=1080][ext=mp4]/bestvideo[height=1080]/bestvideo[ext=mp4]/bestvideo'

    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

    cmd = [
        _yt_dlp_executable(),
        '--format', fmt,
        '-o', template + '.%(ext)s',
        '--no-playlist',
        '--no-part',            # don't use .part temp files
        f'https://www.youtube.com/watch?v={video_id}',
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    log(f'yt-dlp stdout: {proc.stdout[-300:]}')
    if proc.returncode != 0:
        raise RuntimeError(
            'Could not download the video from YouTube. '
            'Make sure the URL is correct and the video is still publicly accessible. '
            f'(yt-dlp exit code {proc.returncode})'
        )

    # Find the downloaded file (extension may vary)
    candidates = glob.glob(template + '.*')
    if not candidates:
        raise RuntimeError(
            'The download finished but no video file was saved. '
            'This is unusual — please try again. If it keeps failing, '
            'make sure yt-dlp is up to date by running:  pip install -U yt-dlp'
        )

    downloaded = candidates[0]
    log(f'Downloaded: {os.path.basename(downloaded)} ({os.path.getsize(downloaded):,} bytes)')

    # Move to the expected output path
    import shutil
    shutil.move(downloaded, output_path)

    log('Video downloaded successfully.')
    return output_path
