<p align="center">
  <h1 align="center">VidCompiler</h1>
  <p align="center">
    Store any file inside a YouTube video — and recover it perfectly.
    <br />
    Survives YouTube's full H.264 re-compression pipeline.
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/codec-H.264%20YUV%204%3A2%3A0-green" alt="H.264 YUV 4:2:0">
  <img src="https://img.shields.io/badge/ECC-Reed--Solomon-orange" alt="Reed-Solomon">
  <img src="https://img.shields.io/badge/pixel%20engine-C%20native-red" alt="C native">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT License">
</p>

---

## How It Works

```
Files  →  tar.gz  →  Reed-Solomon ECC  →  YUV pixel encoding  →  H.264 video  →  YouTube upload
                                                                                        ↓
Files  ←  untar   ←  RS error correction ←  YUV pixel decoding ←  H.264 decode ←  YouTube download
```

1. **Archive** — Input files are packed into a `.tar.gz` archive with CRC-32 integrity check.
2. **Error Correction** — The archive is split into 215-byte chunks, each protected with 40 bytes of Reed-Solomon parity (RS(255, 215)).
3. **Pixel Encoding** — Corrected data is written into YUV 4:2:0 frames: 2 bits per pixel on the Y plane (4 gray levels) and 1 bit per pixel on the Cb/Cr chroma planes.
4. **Video Encoding** — Frames are piped to ffmpeg as raw YUV and encoded to H.264 with hardware acceleration (QSV / NVENC / AMF) or software fallback (libx264).
5. **Audio Steganography** — A backup copy of the file header is embedded in the audio channel using binary FSK modulation (1,500 Hz / 3,000 Hz).
6. **Upload** — The video is uploaded to YouTube as unlisted via the Data API v3.

Decoding reverses the pipeline: download → extract raw frames → decode pixels → Reed-Solomon correct → verify CRC → extract files.

## Features

- **2.5x data density** over v2 by using all three YUV color planes
- **C native pixel engine** (~9.6 MB/s encode, ~7.2 MB/s decode) with automatic NumPy fallback
- **Hardware encoder auto-detection** — Intel QSV, NVIDIA NVENC, AMD AMF, or software libx264
- **FSK audio channel** — header backup survives even if video frames are partially corrupted
- **Backward compatible** — decodes both v3 (`VIDCMPR3`) and legacy v2 (`VIDCMPR2`) videos
- **Web UI** — drag-and-drop files, real-time progress via SSE, one-click decode

## Requirements

- Python 3.10+
- ffmpeg on PATH (or installed via `imageio-ffmpeg`)
- Google account with YouTube Data API v3 credentials
- *(Optional)* C compiler (MSVC, GCC, or Clang) for maximum pixel throughput

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/DaniDevML/video_compiler.git
cd video_compiler
pip install -r requirements.txt

# 2. (Optional) Build the C native library for ~7x faster pixel ops
python native/build.py

# 3. Run
python app.py
```

Open [http://localhost:5000](http://localhost:5000) and follow the one-time OAuth setup in the UI.

## Usage

### Encode & Upload

1. Drop any files or folders onto the drop zone.
2. Optionally set a video title.
3. Click **Encode & Upload to YouTube**.
4. Copy the returned URL — you need it to decode later.

### Decode

1. Switch to the **Decode from URL** tab.
2. Paste the YouTube URL or video ID.
3. Click **Download & Decode**.
4. Download the recovered files as a `.zip`.

> **Note:** YouTube takes 2–5 minutes to finish processing the 1080p version after upload. If decoding fails immediately after uploading, wait and try again.

## Architecture

```
video_compiler/
├── app.py              # Flask web server, job management, SSE streaming
├── video_codec.py      # YUV frame encode/decode primitives (v3 + v2 compat)
├── video_encoder.py    # File → H.264/AAC video pipeline
├── video_decoder.py    # H.264/AAC video → file pipeline
├── audio_codec.py      # FSK steganography encoder/decoder
├── youtube_api.py      # YouTube Data API v3 (OAuth, upload, download)
├── native/
│   ├── frame_ops.c     # C pixel encode/decode (hot path)
│   ├── build.py        # Auto-detect compiler and build DLL/.so
│   └── __init__.py     # ctypes loader with fallback detection
├── static/
│   └── index.html      # Single-page web UI
└── requirements.txt
```

## Technical Specs

| Property | Value |
|---|---|
| Resolution | 1920 x 1080 (YUV 4:2:0) |
| Block size | 4 x 4 px |
| Bits per block | Y: 2 bpp (4 gray levels) / Cb, Cr: 1 bpp |
| Data per frame | 40,140 bytes |
| FPS | 30 |
| Error correction | Reed-Solomon RS(255, 215) — 40 parity bytes/chunk |
| Header redundancy | 5 copies with byte-level majority vote |
| Audio channel | Binary FSK at 100 bps (Goertzel demodulation) |
| Video encoder | H.264 via QSV / NVENC / AMF / libx264 (auto-detected) |

### Performance

| Engine | Encode | Decode | vs NumPy |
|---|---|---|---|
| **C native** (MSVC/GCC/Clang) | **~9.6 MB/s** | **~7.2 MB/s** | **~7x faster** |
| NumPy fallback | ~1.4 MB/s | ~1.1 MB/s | baseline |

> Pixel engine throughput only. End-to-end speed is bounded by ffmpeg encoding and network I/O.

### v3 vs v2

| | v2 | v3 |
|---|---|---|
| Color space | Grayscale (Y only) | YUV 4:2:0 (Y + Cb + Cr) |
| Bits per block | 1 (black/white) | 2 on Y, 1 on Cb/Cr |
| Data per frame | 16,080 B | 40,140 B (**2.5x**) |
| Pixel engine | NumPy only | C native + NumPy fallback |
| Audio channel | — | FSK header backup |
| Header copies | 3 | 5 |

## License

[MIT](LICENSE)
