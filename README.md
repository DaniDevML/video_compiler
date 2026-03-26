# VidCompiler

Store any file inside a YouTube video and recover it perfectly — surviving YouTube's H.264 re-compression.

## How it works

Files are archived, Reed-Solomon error-corrected, and converted to a black-and-white pixel video (each bit = one 4×4 pixel block). The video is uploaded to YouTube as unlisted. To recover the files, paste the URL back into the app — it downloads the video, reads the pixels, error-corrects any YouTube-introduced noise, and reconstructs the original files exactly.

## Requirements

- Python 3.10+
- ffmpeg (must be on PATH, or installed via `imageio-ffmpeg`)
- A Google account + YouTube Data API credentials (see Setup below)

## Installation

```bash
pip install -r requirements.txt
```

## Setup (one time)

1. Go to [Google Cloud Console](https://console.cloud.google.com) and create a project.
2. Enable the **YouTube Data API v3**.
3. Go to **APIs & Services → Credentials** and create an **OAuth 2.0 Client ID** (Desktop App type).
4. Download the JSON file.
5. Start the app — drag and drop the downloaded JSON file onto the yellow setup box in the UI.

The app saves it as `client_secrets.json` locally (git-ignored). The first upload will open a browser window for Google sign-in; the token is cached automatically after that.

## Running

```bash
python app.py
```

Then open [http://localhost:5000](http://localhost:5000) in your browser.

## Usage

**Encode & Upload**
1. Drop any files or folders onto the drop zone.
2. Optionally set a video title.
3. Click **Encode & Upload to YouTube**.
4. Copy the returned URL — you need it to decode later.

**Decode**
1. Switch to the **Decode from URL** tab.
2. Paste the YouTube URL or video ID.
3. Click **Download & Decode**.
4. Download the recovered files as a `.zip`.

> YouTube takes 2–5 minutes to finish processing the 1080p version after upload. If decoding fails immediately after uploading, wait a moment and try again.

## Technical details

| Property | Value |
|---|---|
| Resolution | 1920 × 1080 |
| Block size | 4 × 4 px per bit |
| FPS | 30 |
| Error correction | Reed-Solomon RS(255, 215), 40 parity bytes per chunk |
| Encoder | H.264 QSV / NVENC / AMF / libx264 (auto-detected) |
| Effective data rate | ~4 MB/s encode, ~2 MB/s decode |

## License

For research and personal use.
