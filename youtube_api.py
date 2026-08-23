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


def _close_media(media) -> None:
    """Close the file object behind a MediaFileUpload, if it still has one."""
    for attr in ('_fd', '_file'):
        fh = getattr(media, attr, None)
        if fh is not None and hasattr(fh, 'close'):
            try:
                fh.close()
            except Exception:
                pass


def _read_sidecar(video_path: str) -> str:
    """The header line the encoder wrote next to the video, if present."""
    path = video_path + '.sidecar'
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def upload_video(video_path: str, title: str = 'Data Archive', progress=None) -> tuple:
    """
    Upload a video to YouTube as an unlisted video.

    The header is also written into the description. YouTube stores the
    description verbatim, so it gives the decoder a lossless copy of the
    parameters it would otherwise have to recover from the pixels.

    Returns:
        (video_id, url)
    """
    def log(msg):
        if progress:
            progress(msg)

    log('Authenticating with YouTube API...')
    youtube = get_youtube_service()

    description = ('Encoded data archive. '
                   'Created with vid_compiler for research purposes.')
    sidecar = _read_sidecar(video_path)
    if sidecar:
        description += '\n\n' + sidecar
        log('Header copy embedded in the video description.')

    body = {
        'snippet': {
            'title': title,
            'description': description,
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
    try:
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                log(f'Upload progress: {pct}%')
    finally:
        # MediaFileUpload keeps the source file open for the life of the
        # request. On Windows an open handle blocks deletion outright, so the
        # caller could never clean up its temporary video -- leaking a file the
        # size of the whole upload after every job. Release it here, whether
        # the upload succeeded or not.
        _close_media(media)

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


def yt_dlp_command() -> list:
    """Return an argv prefix that runs yt-dlp.

    Prefers running it as a module through the current interpreter, which works
    whenever the package is importable and does not depend on the console
    script landing somewhere on PATH. Windows Store Python installs scripts
    under a per-version LocalCache directory that is usually not on PATH, so
    hunting for the .exe is the fragile path, not the reliable one.
    """
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, '-m', 'yt_dlp']
    except ImportError:
        pass

    exe = shutil.which('yt-dlp')
    if exe:
        return [exe]

    import glob
    candidates = [os.path.join(os.path.dirname(sys.executable), 'Scripts',
                               'yt-dlp.exe')]
    candidates += glob.glob(os.path.join(
        os.environ.get('LOCALAPPDATA', ''), 'Packages',
        'PythonSoftwareFoundation.Python.*', 'LocalCache', 'local-packages',
        'Python*', 'Scripts', 'yt-dlp.exe'))
    for c in candidates:
        if os.path.exists(c):
            return [c]

    raise FileNotFoundError(
        'The video downloader (yt-dlp) is not installed. '
        'Open a terminal and run:  pip install yt-dlp  — then restart the app.'
    )


def _yt_dlp_executable() -> str:
    """Backwards-compatible shim for callers expecting a single path."""
    return yt_dlp_command()[-1]


def fetch_description(url: str) -> str:
    """Fetch a video's description text.

    Best-effort: the description is only an accelerator for decoding, so any
    failure here is not worth surfacing -- the decoder falls back to reading
    the header out of the frames.
    """
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        video_id = extract_video_id(url)
        proc = subprocess.run(
            [*yt_dlp_command(), '--skip-download', '--no-playlist',
             '--print', 'description',
             f'https://www.youtube.com/watch?v={video_id}'],
            capture_output=True, text=True, timeout=60, **kwargs)
        return proc.stdout if proc.returncode == 0 else ''
    except Exception:
        return ''


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
        *yt_dlp_command(),
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
