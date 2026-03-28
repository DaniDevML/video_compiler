# VidCompiler

Store any file inside a YouTube video and recover it perfectly — surviving YouTube's H.264 re-compression.

## How it works

Files are archived, Reed-Solomon error-corrected, and encoded into a YUV 4:2:0 video using multiple gray levels per pixel block. All three color planes (Y, Cb, Cr) carry data, and a header copy is embedded in the audio channel for extra robustness. The video is uploaded to YouTube as unlisted. To recover the files, paste the URL back into the app — it downloads the video, reads the pixels, error-corrects any YouTube-introduced noise, and reconstructs the original files exactly.

## Requirements

- Python 3.10+
- ffmpeg (must be on PATH, or installed via `imageio-ffmpeg`)
- A Google account + YouTube Data API credentials (see Setup below)
- (Optional) C compiler for maximum speed — MSVC, GCC, or Clang

## Installation

```bash
pip install -r requirements.txt
```

### Optional: build the C native library

The pixel encode/decode hot paths have a C implementation that runs significantly faster than the NumPy fallback. Build it once:

```bash
python native/build.py
```

This auto-detects MSVC / GCC / Clang. If no compiler is found the app works fine using NumPy — just slower.

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

> YouTube takes 2-5 minutes to finish processing the 1080p version after upload. If decoding fails immediately after uploading, wait a moment and try again.

## Technical details

| Property | Value |
|---|---|
| Resolution | 1920 x 1080 (YUV 4:2:0) |
| Block size | 4 x 4 px |
| Bits per block | Y plane: 2 bpp (4 gray levels), Cb/Cr planes: 1 bpp |
| Data per frame | 40,140 bytes (2.5x more than v2) |
| FPS | 30 |
| Error correction | Reed-Solomon RS(255, 215), 40 parity bytes per chunk |
| Header redundancy | 5 copies with majority vote |
| Audio channel | FSK steganography (header backup) |
| Video encoder | H.264 QSV / NVENC / AMF / libx264 (auto-detected) |
| Pixel engine | C native library (ctypes) or NumPy fallback |
| Backward compat | Decodes both v3 (VIDCMPR3) and v2 (VIDCMPR2) videos |

### v3 vs v2 comparison

| | v2 (dev branch) | v3 (optimization branch) |
|---|---|---|
| Color space | Grayscale | YUV 4:2:0 (all 3 planes) |
| Bits per block | 1 (black/white) | 2 on Y, 1 on Cb/Cr |
| Data per frame | 16,080 bytes | 40,140 bytes |
| Pixel engine | Python/NumPy only | C native + NumPy fallback |
| Audio channel | Not used | FSK header backup |
| Header copies | 3 | 5 |

### Benchmarked throughput (pixel encode/decode only)

| Engine | Encode | Decode |
|---|---|---|
| C native (MSVC/GCC/Clang) | **~9.6 MB/s** | **~7.2 MB/s** |
| NumPy fallback | ~1.4 MB/s | ~1.1 MB/s |

> Measured on a 2 MB payload. End-to-end speed (including ffmpeg, RS coding, and YouTube upload/download) is limited by the video pipeline and network, not the pixel engine.

## License

For research and personal use.
