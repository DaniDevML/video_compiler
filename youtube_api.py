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

# youtube.upload is enough to publish a video, but not to create a playlist or
# add items to one -- those need the broader manage scope. Both are requested so
# a single consent covers the whole flow; if only the narrow one was granted
# previously, the app falls back to handing back individual video links.
SCOPE_UPLOAD = 'https://www.googleapis.com/auth/youtube.upload'
SCOPE_MANAGE = 'https://www.googleapis.com/auth/youtube'
SCOPES = [SCOPE_UPLOAD, SCOPE_MANAGE]
# Credentials belong to the user and must outlive the process. In a frozen
# build __file__ points inside the unpack directory, which is deleted on exit,
# so anything written there would vanish.
from paths import data as _data

TOKEN_FILE = _data('yt_token.pickle')
CLIENT_SECRETS_FILE = _data('client_secrets.json')


def token_scopes() -> list:
    """Scopes the saved token actually carries, if there is one."""
    try:
        with open(TOKEN_FILE, 'rb') as f:
            return list(pickle.load(f).scopes or [])
    except Exception:
        return []


def can_manage_playlists() -> bool:
    """Whether the saved token is allowed to create playlists."""
    return SCOPE_MANAGE in token_scopes()


def get_youtube_service(require_manage: bool = False):
    """Return an authenticated YouTube API service object.

    `require_manage` forces re-consent when the saved token only carries the
    upload scope, which is what a token issued before playlist support looks
    like. Re-consent opens a browser, so it is only demanded when the caller
    genuinely needs the wider permission.
    """
    creds = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as f:
            creds = pickle.load(f)

    if creds and require_manage and SCOPE_MANAGE not in (creds.scopes or []):
        creds = None          # force the consent screen

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
    """Return an argv prefix that runs yt-dlp as a subprocess.

    Kept for callers that still shell out (the benchmarks). The application
    itself uses the Python API below instead, because a frozen build has no
    interpreter to hand: sys.executable is the application, so spawning
    "sys.executable -m yt_dlp" would relaunch the whole app.
    """
    if getattr(sys, 'frozen', False):
        exe = shutil.which('yt-dlp')
        if exe:
            return [exe]
        raise FileNotFoundError(
            'yt-dlp is not available as a separate program. The frozen build '
            'uses the bundled library directly; this path is only for '
            'development use.')
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
        'Open a terminal and run:  pip install yt-dlp')


def _yt_dlp_executable() -> str:
    """Backwards-compatible shim for callers expecting a single path."""
    return yt_dlp_command()[-1]


def _ydl(opts: dict):
    """A YoutubeDL configured quietly, using the library in this process."""
    import yt_dlp
    base = {
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'noplaylist': True,
        'logger': _NullLogger(),
    }
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


class _NullLogger:
    """yt-dlp writes to stdout by default; a windowed build has no stdout."""

    def debug(self, msg): pass

    def info(self, msg): pass

    def warning(self, msg): pass

    def error(self, msg): pass


def video_info(url: str) -> dict:
    """Metadata for a video without downloading it."""
    with _ydl({'skip_download': True}) as ydl:
        return ydl.extract_info(url, download=False) or {}


def fetch_description(url: str) -> str:
    """Fetch a video's description text.

    Best-effort: the description is only an accelerator for decoding, so any
    failure here is not worth surfacing -- the decoder falls back to reading
    the header out of the frames.
    """
    try:
        video_id = extract_video_id(url)
        info = video_info(f'https://www.youtube.com/watch?v={video_id}')
        return info.get('description') or ''
    except Exception:
        return ''


def download_video(url: str, output_path: str, progress=None) -> str:
    """
    Download a YouTube video at 1080p. Returns the path actually written.

    Uses yt-dlp as a library rather than a subprocess. A frozen build has no
    interpreter to spawn -- sys.executable is the application itself -- so
    shelling out would relaunch the whole app instead of downloading anything.
    """
    def log(msg):
        if progress:
            progress(msg)

    import glob
    import tempfile

    video_id = extract_video_id(url)
    log(f'Downloading video {video_id}...')

    dl_dir = tempfile.mkdtemp()
    template = os.path.join(dl_dir, 'video')

    # Video-only: the frames carry the payload, and skipping the audio stream
    # roughly halves what has to come down the wire.
    fmt = ('bestvideo[height=1080][ext=mp4]/bestvideo[height=1080]/'
           'bestvideo[ext=mp4]/bestvideo')

    with _ydl({'format': fmt,
               'outtmpl': template + '.%(ext)s',
               'overwrites': True,
               'retries': 5,
               'fragment_retries': 5}) as ydl:
        ydl.download([f'https://www.youtube.com/watch?v={video_id}'])

    candidates = glob.glob(template + '.*')
    if not candidates:
        raise RuntimeError(
            'The download finished but no video file was saved. '
            'This is unusual - please try again.'
        )

    downloaded = candidates[0]
    log(f'Downloaded: {os.path.basename(downloaded)} '
        f'({os.path.getsize(downloaded):,} bytes)')

    shutil.move(downloaded, output_path)
    log('Video downloaded successfully.')
    return output_path


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------
#
# An archive split across several videos otherwise leaves the user holding a
# list of URLs, every one of which is required -- lose one and the archive is
# gone. A playlist collapses that to a single link that also records the order,
# which is exactly the information the decoder needs.

_PLAYLIST_RE = re.compile(r'[?&]list=([A-Za-z0-9_-]{10,})')


def is_playlist_url(url: str) -> bool:
    """Whether this looks like a link to a playlist rather than one video."""
    return bool(_PLAYLIST_RE.search(url or ''))


def extract_playlist_id(url: str) -> str:
    m = _PLAYLIST_RE.search(url or '')
    if m:
        return m.group(1)
    if re.fullmatch(r'(PL|UU|LL|FL|OL)[A-Za-z0-9_-]{10,}', (url or '').strip()):
        return url.strip()
    raise ValueError('That does not look like a YouTube playlist link.')


def playlist_url(playlist_id: str) -> str:
    return f'https://www.youtube.com/playlist?list={playlist_id}'


def create_playlist(title: str, description: str = '',
                    progress=None) -> str:
    """Create an unlisted playlist and return its id."""
    def log(msg):
        if progress:
            progress(msg)

    youtube = get_youtube_service(require_manage=True)
    log('Creating playlist...')
    resp = youtube.playlists().insert(
        part='snippet,status',
        body={
            'snippet': {'title': title, 'description': description},
            'status': {'privacyStatus': 'unlisted'},
        },
    ).execute()
    return resp['id']


def add_to_playlist(playlist_id: str, video_id: str, position: int = None):
    """Append a video to a playlist, optionally at a fixed position."""
    youtube = get_youtube_service(require_manage=True)
    snippet = {
        'playlistId': playlist_id,
        'resourceId': {'kind': 'youtube#video', 'videoId': video_id},
    }
    if position is not None:
        snippet['position'] = position
    return youtube.playlistItems().insert(
        part='snippet', body={'snippet': snippet}).execute()


def expand_playlist(url: str, progress=None) -> list:
    """Every video URL in a playlist, in order.

    Read with yt-dlp rather than the Data API: a playlist that is unlisted is
    still readable by link, so decoding needs no credentials at all. Requiring
    OAuth to *read* something the link already grants would be a poor trade.
    """
    def log(msg):
        if progress:
            progress(msg)

    pid = extract_playlist_id(url)
    log('Reading playlist...')
    # extract_flat keeps this to one metadata request instead of one per video,
    # and noplaylist must be off or yt-dlp resolves the link to a single entry.
    with _ydl({'extract_flat': 'in_playlist',
               'skip_download': True,
               'noplaylist': False}) as ydl:
        info = ydl.extract_info(playlist_url(pid), download=False) or {}

    urls = []
    for entry in info.get('entries') or []:
        if not entry:
            continue
        vid = entry.get('id')
        if vid:
            urls.append(f'https://www.youtube.com/watch?v={vid}')
    if not urls:
        raise RuntimeError(
            'That playlist appears to be empty, or is private. A playlist '
            'holding an archive must be public or unlisted to be readable.')
    log(f'Playlist holds {len(urls)} video(s).')
    return urls
