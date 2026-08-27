<p align="center">
  <h1 align="center">VidCompiler</h1>
  <p align="center">
    Store any file inside a YouTube video — and recover it perfectly.
    <br />
    Format verified by real upload/download round trips, not simulation.
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/codec-H.264%20YUV%204%3A2%3A0-green" alt="H.264 YUV 4:2:0">
  <img src="https://img.shields.io/badge/ECC-Reed--Solomon-orange" alt="Reed-Solomon">
  <img src="https://img.shields.io/badge/pixel%20engine-C%20native-red" alt="C native">
  <img src="https://img.shields.io/badge/license-GPL--3.0-lightgrey" alt="GPL-3.0 License">
</p>

**Contents** — [Branches](#branches) · [What each version can and cannot do](#what-each-version-can-and-cannot-do) · [How it works](#how-it-works) · [Quick start](#quick-start) · [Usage](#usage) · [Technical specs](#technical-specs) · [Performance](#performance) · [The v5 format](#the-v5-format-and-why-v4-had-to-change) · [Tests](#tests) · [License](#license)

---

## Branches

| branch | codec | what it adds |
|---|:--:|---|
| [`dev`](../../tree/dev) | v3 | the original single-video pipeline: C pixel engine, FSK audio header, web UI |
| [`optimization-v2`](../../tree/optimization-v2) | v3 | identical in content to `dev`; kept only for history |
| [`optimization-v3`](../../tree/optimization-v3) | v4 | the density experiment — 3 bpp on luma. Measured against real YouTube, it loses data |
| **`v5-max-throughput`** (this one) | **v5** | the first format chosen from real round trips, plus a native Reed-Solomon codec |
| [`v6-parallel`](../../tree/v6-parallel) | v5 | sharding across concurrently uploaded videos, and a Windows executable |
| [`v7-playlists`](../../tree/v7-playlists) | v5 | one playlist link per archive, a portable Docker image, faster archiving |
| [`file_explorer`](../../tree/file_explorer) | v5 | a browsable file manager on top of v7, with encryption before upload |
| [`v8-dense-audio`](../../tree/v8-dense-audio) | v8 | 3 levels per luma block instead of 2, and payload carried in the audio track |

v5 through `file_explorer` share one frame format — a video encoded on
any of them decodes on all of them, and what those branches add is
everything *around* the format. `v8-dense-audio` is the first change to
the format itself since v5; it writes a different header, and readers
from v5 onward handle both.

## What each version can and cannot do

The same table appears on every branch. This one is **`v5-max-throughput`**.

| | `dev`<br>v3 | `optimization-v3`<br>v4 | `v5-max-`<br>`throughput` | `v6-parallel` | `v7-playlists` | `file_explorer` | `v8-dense-`<br>`audio` |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Data per frame | 40,140 B | 64,200 B | 56,100 B | 56,100 B | 56,100 B | 56,100 B | 166,540 B |
| Recovers a file that really went through YouTube | untested | **no** | **yes** | yes | yes | yes | probe only |
| Verified byte-identical at 1 GB | no | no | **yes** | yes | yes | yes | local only |
| Threaded, packed-bit pixel engine | no | no | **yes** | yes | yes | yes | yes |
| Native C Reed-Solomon (32x decode) | no | no | **yes** | yes | yes | yes | yes |
| Header in audio *and* description | no | no | **yes** | yes | yes | yes | yes |
| Selectable density profiles | no | no | **yes** | yes | yes | yes | yes |
| Archives larger than one video | no | no | no | yes | yes | yes | yes |
| One link for a split archive | no | no | no | no | yes | yes | yes |
| Windows executable | no | no | no | yes | yes | yes | yes |
| Docker image | no | no | no | no | yes | yes | yes |
| Browsable file manager | no | no | no | no | no | yes | yes |
| Encryption before upload | no | no | no | no | no | yes | yes |
| Level counts beyond powers of two | no | no | no | no | no | no | yes |
| Payload carried in the audio track | no | no | no | no | no | no | yes |

> "Verified byte-identical at 1 GB" means a single 1 GB video, uploaded to
> YouTube and downloaded back. The **sharded** 1 GB path on `v6-parallel` and
> later is not yet verified end to end — see *the error-correction margin*.
>
> `v8-dense-audio` is the exception and is marked accordingly. Its *format*
> was chosen from a real YouTube upload, which measured the error rate of
> every level count directly, but a complete v8 file has not yet made the
> round trip through the service. Its 1 GB and 3 GB round trips are local.

### What this branch can do

- **Recover a file that really went through YouTube**, verified by upload and
  download against the live service at 8 MB, 64 MB, 256 MB and **1 GB** — all
  byte-identical. This is the first branch of which that is true.
- Carry 56,100 bytes per frame with a format chosen from those measurements:
  2 bits per block on luma, 3 on chroma, the reverse of what v4 assumed.
- Decode 32x faster than v4 on real payloads, via a native C Reed-Solomon
  codec, and encode and decode pixels on a threaded engine that works directly
  on packed bits.
- Recover the header three independent ways — from the frames, from 4-FSK tones
  in the audio track, and from a sidecar in the video description.
- Swap density with a `Profile`, which sets block size and bits per block
  independently per plane. The header records both, so any profile decodes.

### What it cannot do

- **Store more than about 1.28 GB in one go.** One archive is one video, and
  YouTube's 15-minute cap on unverified accounts sets the ceiling.
  [`v6-parallel`](../../tree/v6-parallel) lifts it by splitting across videos.
- **Upload at more than one stream's throughput.** A single HTTP upload
  measured 18.6 Mbit/s where three concurrent ones reached 39.9. Upload
  dominates end-to-end time at any real size, so this is the branch's main
  practical limit.
- Leave much error-correction headroom: a healthy round trip already spends
  ~14% of its blocks on repair. See *the error-correction margin* below.
- No executable, no Docker image, no playlist support.

## How It Works

```
Files  →  tar.gz  →  Reed-Solomon ECC  →  YUV pixel encoding  →  H.264 video  →  YouTube upload
                                                                                        ↓
Files  ←  untar   ←  RS error correction ←  YUV pixel decoding ←  H.264 decode ←  YouTube download
```

1. **Archive** — Input files are packed into a `.tar.gz` archive with CRC-32 integrity check.
2. **Error Correction** — The archive is split into 215-byte chunks, each protected with 40 bytes of Reed-Solomon parity (RS(255, 215)).
3. **Pixel Encoding** — Corrected data is written into YUV 4:2:0 frames: 2 bits per 4x4 block on the Y plane and 3 bits per 4x4 block on the Cb/Cr chroma planes. The split is deliberate and measured — see *The v5 format* below.
4. **Video Encoding** — Frames are piped to ffmpeg as raw YUV and encoded to H.264 with hardware acceleration (NVENC / QSV / AMF) or software fallback (libx264).
5. **Side channels** — A copy of the header goes into the audio track as FSK tones *and* into the video description, so the decoder can recover the archive parameters three independent ways.
6. **Upload** — The video is uploaded to YouTube as unlisted via the Data API v3.

Decoding reverses the pipeline: download → extract raw frames → decode pixels → Reed-Solomon correct → verify CRC → extract files.

## Features

- **C native pixel engine**, multithreaded, operating directly on packed bits
- **Hardware encoder auto-detection** — NVIDIA NVENC, Intel QSV, AMD AMF, or software libx264
- **Three independent header channels** — pixels, FSK audio, and the video description
- **Selectable density** — a `Profile` sets block size and bits per block independently per plane; the header records both, so any profile decodes
- **Backward compatible** — decodes v4 (`VIDCMPR4`), v3 (`VIDCMPR3`), and legacy v2 (`VIDCMPR2`) videos
- **Web UI** — drag-and-drop files, real-time progress via SSE, one-click decode

## Requirements

- Python 3.10+
- ffmpeg on PATH (or installed via `imageio-ffmpeg`)
- Google account with YouTube Data API v3 credentials
- *(Optional)* C compiler (GCC/MinGW, MSVC, or Clang) for the fast pixel path

## Quick Start

```bash
git clone https://github.com/DaniDevML/video_compiler.git
```

```bash
pip install -r requirements.txt
```

Build the native library (optional but strongly recommended — it is ~20x faster than the NumPy fallback):

```bash
python native/build.py
```

Then run the app and open [http://localhost:5000](http://localhost:5000):

```bash
python app.py
```

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
├── video_codec.py      # YUV frame encode/decode primitives + sidecar header
├── video_encoder.py    # File → H.264/AAC video pipeline
├── video_decoder.py    # H.264/AAC video → file pipeline
├── audio_codec.py      # FSK steganography encoder/decoder
├── youtube_api.py      # YouTube Data API v3 (OAuth, upload, download)
├── native/
│   ├── frame_ops.c     # C pixel encode/decode (hot path)
│   ├── build.py        # Auto-detect compiler and build DLL/.so
│   └── __init__.py     # ctypes loader with fallback detection
├── bench/              # Benchmarks and test suites (see below)
└── static/index.html   # Single-page web UI
```

## Technical Specs

| Property | Value |
|---|---|
| Resolution | 1920 x 1080 (YUV 4:2:0) |
| Block size | 4 x 4 px (both planes) |
| Bits per block | Y: 2 bpp (4 levels) / Cb, Cr: 3 bpp (8 levels) |
| Data per frame | 56,100 bytes (`PROFILE_DENSE`: 128,880) |
| FPS | 30 |
| Error correction | Reed-Solomon RS(255, 215) — 40 parity bytes/chunk |
| Header redundancy | 5 in-frame copies + audio track + video description |
| Audio channel | Binary FSK at 2,100 bps, 3/6 kHz tones (4-FSK on `v6-parallel`) |
| Upload quantiser | constqp 44 (see *the quantiser cliff* below) |
| Video encoder | H.264 via NVENC / QSV / AMF / libx264 (auto-detected) |

---

# Performance

All numbers below were produced by the scripts in `bench/` on the machine
listed under *Test system*. Nothing here is estimated. Re-run any of them to
reproduce.

**Test system:** Intel Core i7-8700 (6C/12T), NVIDIA GTX 1060 6 GB, Windows 10,
Python 3.11, ffmpeg 2026-01-14. Payloads are incompressible random bytes — the
worst case, since gzip cannot shrink them before encoding.

## End-to-end: v4 vs v5

`bench/bench_e2e.py`, best of 2–3 runs, byte-identity verified by SHA-256 on
every run.

> These figures were taken while v5 still used v4's 64,200 B/frame geometry, so
> they isolate the *engineering* changes (pixel engine, quantiser, decode
> architecture) from the later format change. The format now carries 56,100 B
> per frame, which shifts the absolute times and the video size a little; the
> real-YouTube table above is the current end-to-end reference.

| Payload | | encode | decode | uploaded video |
|---|---|---|---|---|
| **4 MB** | v4 | 1.21 s | 2.51 s | 23.67 MB |
| | v5 | **0.75 s** | **0.75 s** | **12.45 MB** |
| | | 1.61x | 3.35x | 1.90x smaller |
| **8 MB** | v4 | 2.17 s | 3.92 s | 47.37 MB |
| | v5 | **1.21 s** | **1.17 s** | **24.90 MB** |
| | | 1.79x | 3.35x | 1.90x smaller |
| **16 MB** | v4 | 4.19 s | 7.20 s | 94.72 MB |
| | v5 | **2.14 s** | **1.96 s** | **49.77 MB** |
| | | 1.96x | 3.67x | 1.90x smaller |

In throughput terms at 16 MB: encode went from 4.00 to 7.82 MB/s, decode from
2.33 to 8.57 MB/s.

**The 1.90x size reduction is the number that matters most in practice.** Real
upload and download time is network-bound, not CPU-bound: on a 20 Mbit/s
uplink, the 8 MB case spends ~19 s uploading under v4 against ~10 s under v5,
which dwarfs the ~1 s of encode time either way.

## Real YouTube round trips

`bench/bench_youtube.py` runs the whole thing against the live service: encode,
upload, wait for the 1080p rendition, download, decode, compare SHA-256. This
is the only measurement that actually proves the format works.

| stage | 8 MB payload |
|---|---|
| encode | 1.4 s → 26.5 MB video (3.16x) |
| upload | 18.2 s (11.6 Mbit/s) |
| YouTube processing to 1080p | 24 s |
| download | 5.0 s — YouTube served 20.6 MB |
| decode + error correction | 1.5 s |
| **result** | **bytes identical** |

### Scaling, to 1 GB

| payload | video | upload | processing | download | decode | result |
|---|---|---|---|---|---|---|
| 8 MB | 26.5 MB | 18.2 s @ 11.6 Mbit/s | 24 s | 5.0 s | **1.5 s** | identical |
| 64 MB | 202 MB | 108 s @ 15.7 Mbit/s | 50 s | 8.5 s | 352.7 s* | identical |
| 256 MB | 808 MB | 369 s @ 18.4 Mbit/s | 91 s | 30.6 s | **38.8 s** | identical |
| **1 GB** | **3.38 GB** | **1441 s @ 18.8 Mbit/s** | 246 s | 149 s | **156.5 s** | **identical** |

\* the 64 MB decode predates the native Reed-Solomon decoder. Re-measured on
the same 256 MB video, decode went from **1252 s to 38.8 s — 32x faster**.

A 1 GB archive becomes an 11-minute 1080p video. Note that puts a ceiling on a
single video: YouTube caps unverified accounts at 15 minutes, which at this
profile is about 1.28 GB (56,100 B/frame x 30 fps x 900 s x 215/255).

**YouTube returns less than it was given** — 2.44 GB back from a 3.38 GB
upload. It re-encodes everything, which is why the upload quantiser can be
raised at all, and why a format has to be validated against the service rather
than against our own encoder.

**How hard error correction works.** The decoder records it: on the 8 MB round
trip, 5,353 of 39,031 blocks needed repair (13.71%), averaging 1.28 corrected
symbols each against a per-block capacity of 20. Healthy, but the tail matters
— see the quantiser note below.

### The upload quantiser has a cliff

Raising the quantiser shrinks the upload for free, until abruptly it does not.
Through our own decoder every setting up to qp51 round-trips perfectly. Through
real YouTube:

| upload | expansion | our decoder | real YouTube |
|---|---|---|---|
| qp 44 | 3.16x | clean | **clean** (verified at 8/64/256/1024 MB) |
| qp 51 | 2.75x | clean | **fails** — 22 of 39,031 blocks beyond repair |

Uploading a coarser file degrades the source YouTube re-encodes *from*. The
15% saving at qp51 is not worth the failure, so qp44 ships. This is the clearest
illustration of why the local benchmarks cannot settle a format question.

## Where the gains came from

### Pixel engine — 4.2x encode, 13.2x decode

`bench/bench_pixel.py`. Both the old and new C engines are compiled here with
the same `gcc -O3 -march=native`, so this is a like-for-like comparison of the
code rather than of compilers.

| Engine | encode | decode |
|---|---|---|
| NumPy fallback | 4.9 MB/s | 5.7 MB/s |
| v4 C (single-threaded, byte-per-bit) | 25.4 MB/s | 18.5 MB/s |
| v5 C (threaded, byte-per-bit) | 95.2 MB/s | 191.3 MB/s |
| **v5 C (threaded, packed bits)** | **107.8 MB/s** | **244.4 MB/s** |

Three changes: the loops are parallelised across frames with OpenMP; block
rows are built once and replicated with 64-bit stores instead of filling
byte-at-a-time; and the payload is read as packed bits rather than one byte per
bit, which removes an 8x intermediate array (75 MB for an 8 MB payload) from
both sides of the pipeline.

### Upload size — 1.90x smaller, for free

`bench/bench_upload_preset.py`, `bench/sweep_upload_qp.py`. The v4 pipeline
uploaded at `qp 18`. The encoded blocks are flat by construction, so a much
coarser quantiser still reproduces them exactly:

| upload setting | expansion | bit errors |
|---|---|---|
| qp 18 (v4 default) | 4.76x | 0 |
| qp 34 | 3.27x | 0 |
| **qp 44 (v5)** | **2.50x** | **0** |
| qp 47 | 2.30x | 1.7e-05 |
| qp 50 | 2.16x | 5.9e-04 |

Raising the quantiser costs nothing on the far side either: measured through a
simulated YouTube transcode, the recovered error rate is **flat** from qp 18 to
qp 46 (2.39e-01 → 2.43e-01), because YouTube discards our bitstream and
re-encodes from scratch regardless of how carefully we encoded it.

The low-latency NVENC presets (`llhp`) also encode this content at the same
size but noticeably faster than the default preset.

### Decode architecture — 3.4x

Profiling (`bench/profile_decode.py`) showed decode was 94% ffmpeg and only
1.4% pixel work, because the video was being **fully decoded twice** — once to
detect the format and once to read the payload — with ~490 MB of raw YUV staged
to disk each time. Three fixes:

- **Detect on a prefix.** Format detection now decodes only the first 16
  frames instead of the entire video.
- **Stream instead of staging.** Frames are piped straight from ffmpeg rather
  than written to a temp file and read back.
- **Drop CUDA, drop the scale filter.** Measured on this machine
  (`bench/bench_decode_cmd.py`): CPU decode runs at 250 fps against 142 fps
  with `-hwaccel cuda`, because hwaccel has to copy every frame back over PCIe
  to reach system memory. The scale filter was a full-frame no-op whenever the
  video is already 1080p.
- **Batch size.** A batch is read in full before pixel work starts, so batch
  size sets how long ffmpeg sits blocked on a full pipe. 158 fps at 4 frames
  per batch against 62 at 32 and 34 at 64.

### Reed-Solomon — 55x decode, 3x encode

`bench/bench_rs.py`, `bench/test_rs_native.py`. `native/rs.c` implements the
same code galois does — table-driven GF(2^8), syndromes, Berlekamp-Massey,
Chien search, Forney — parallelised across chunks.

| operation | galois | native | |
|---|---|---|---|
| decode | 0.8 MB/s | **44.2 MB/s** | 55x |
| encode | 52 MB/s | **153 MB/s** | 3x |

This is the single largest win in the project, because decode is where the
time actually went. Off YouTube the payload always carries some errors, so the
cheap path (strip parity, check CRC) never fires on a large archive and every
block goes through correction. Measured on the same 256 MB video: **1252 s
before, 38.8 s after**.

Matching galois bit-for-bit matters, since existing videos were encoded with
it. `bench/test_rs_native.py` checks agreement at every error count from zero
to the correction limit, past it, and on burst patterns, and confirms an
uncorrectable block is *reported* rather than silently returned as wrong bytes.
galois remains the fallback when the C library is unavailable.

---

# The v5 format, and why v4 had to change

**The v4 format did not survive YouTube.** A real 8 MB round trip failed its
integrity check after error correction. This was not a marginal failure: the
luma plane came back with a 3.6e-02 bit error rate, far past what RS can
repair. v4 had only ever been validated against our own encoder, which
reproduces the blocks exactly and therefore proves nothing about the service.

Three probe uploads then measured the real channel. Each test video is built
from consecutive segments modulated at different densities, so a single round
trip measures a whole ladder (`bench/diag_youtube_formats.py`).

## What the real channel does

| format | B/frame | overall | Y | Cb | Cr |
|---|---|---|---|---|---|
| Y 8px/2bpp + C 8px/1bpp | 9,930 | 0 | 0 | 0 | 0 |
| Y 4px/1bpp + C 4px/1bpp | 24,060 | 0 | 0 | 0 | 0 |
| Y 4px/2bpp + C 4px/2bpp | 48,120 | 2.3e-05 | 3.4e-05 | 0 | 0 |
| **Y 4px/3bpp + C 4px/2bpp** (v4) | **64,200** | **3.6e-02** | **4.8e-02** | 0 | 0 |
| Y 4px/4bpp + C 4px/3bpp | 88,260 | 1.4e-01 | 1.9e-01 | 0 | 0 |

Two findings, neither of which the simulation predicted:

1. **Chroma is far more robust than luma.** It came back *bit-perfect* at every
   density tried, up to 3 bits per block. Every failure above is a luma
   failure. v4 broke for exactly one reason: 3 bpp on the Y plane.
2. **Luma needs contrast, not area.** 2x2 blocks at 1 bpp -- pure black and
   white -- returned zero errors, while 4x4 blocks at 3 bpp did not. What
   survives a transcode is the spacing between levels, not the size of a block.

## Density and upload size pull opposite ways

Following those findings upward gives formats carrying far more per frame. But
high-contrast 2x2 blocks are the most expensive thing a video codec can be
asked to represent, so the frames themselves get much bigger
(`bench/bench_profile_size.py`):

| profile | B/frame | expansion | YouTube BER |
|---|---|---|---|
| **Y 4px/2bpp + C 4px/3bpp** (default) | 56,100 | **2.34x** | 1.1e-05 |
| Y 4px/2bpp + C 2px/2bpp | 96,480 | 3.15x | 4.3e-07 |
| Y 2px/1bpp + C 2px/2bpp (`PROFILE_DENSE`) | 128,880 | 4.81x | 1.6e-07 |

End-to-end time is dominated by bytes on the wire, not frame count. On the
measured 12 Mbit/s uplink an 8 MB payload costs about 12.5 s of upload at 2.34x
against 25.7 s at 4.81x, while the extra frames of the sparse profile cost well
under a second of CPU. So **the default optimises for total bytes**, and
`PROFILE_DENSE` is exposed for the case where frame count is what matters --
fitting a very large archive inside YouTube's per-video duration limit.

The default carries 87% of v4's bytes per frame, uploads *smaller* per payload
byte than v4 did, and unlike v4 it actually round-trips.

## Things that were tried and did not work

- **Adaptive demodulation.** `bench/sweep_adaptive.py` recalibrated the slicer
  per frame from the observed level distribution, on the theory that the
  transcode applies a gain shift the fixed thresholds miss. It changed nothing
  (1.65e-01 fixed against 1.65e-01 adaptive) -- the damage is per-block
  information loss, not a correctable level shift.
- **Trusting the simulation.** The local ffmpeg channel model in
  `bench/channel.py` did correctly predict that v4 would fail, but for the
  wrong reason: it mangles chroma, which the real service leaves untouched. It
  is kept for fast iteration, but no format decision should rest on it.

## The side channels, in proportion

The audio track and the video description are genuinely useful, but not for
capacity — and it is worth being precise about the scale:

| channel | payload rate | share of total |
|---|---|---|
| Pixels | 1,926,000 bytes/s | 99.989% |
| Audio (FSK) | 215 bytes/s | 0.011% |
| Description | ~3.7 KB, one-off | ~0% |

Their real value is redundancy and speed of decode:

- **Video description.** YouTube stores it verbatim, making it the one
  perfectly lossless channel available. v5 puts a base64 header there, so the
  decoder learns the archive size, frame count and CRC without decoding and
  scanning any frames. Malformed or absent descriptions fall back cleanly.
- **Audio track.** v4 wrote an FSK header copy but **nothing ever read it**,
  and a length-framing bug made exact recovery impossible regardless — the
  decoder could not distinguish payload from the zero padding added to fill the
  last RS chunk. v5 fixes the framing, wires it in as a last-resort fallback,
  and raises the symbol rate from 100 to 2,100 bps after verifying exact
  recovery through a real AAC round-trip down to 64 kbps.

---

# Tests

```bash
python bench/run_all.py
```

| suite | covers |
|---|---|
| `test_native.py` | C engine matches NumPy pixel-for-pixel; packed and byte-per-bit paths agree; survives noise up to half the level spacing |
| `test_e2e.py` | Files → video → files byte-identical, across empty, tiny, compressible, incompressible and multi-file payloads |
| `test_sidecar.py` | Description header round-trips; malformed input never raises; decoding identical with, without, and with a corrupt sidecar |
| `test_audio.py` | Exact recovery through real AAC at 128/96/64 kbps, across RS chunk boundaries |
| `test_audio_fallback.py` | Header recoverable from the audio track alone |
| `test_compat.py` | Videos encoded by the previous release still decode byte-identically |

The benchmark and sweep scripts (`bench_*.py`, `sweep_*.py`, `diag_*.py`,
`profile_*.py`) are separate from the tests and reproduce every number in this
README.

## Version History

| | v2 | v3 | v4 | v5 |
|---|---|---|---|---|
| Colour space | Grayscale | YUV 4:2:0 | YUV 4:2:0 | YUV 4:2:0 |
| Bits per block | 1 | 2 Y / 1 C | 3 Y / 2 C | **2 Y / 3 C** |
| Data per frame | 16,080 B | 40,140 B | 64,200 B | 56,100 B |
| Survives real YouTube | untested | untested | **no** (3.6e-02 BER) | **yes** (1.1e-05) |
| Format chosen by | — | — | local encoder only | **real round trips** |
| Pixel engine | NumPy | C single-thread | C single-thread | C threaded, packed bits |
| Reed-Solomon | galois | galois | galois | **native C (55x decode)** |
| Upload quantiser | qp 18 | qp 18 | qp 18 | **qp 44** |
| Decode passes | 2 | 2 | 2 | **1 (streamed)** |
| Audio channel | — | header (never read) | header (never read) | **2,100 bps, read as fallback** |
| Description channel | — | — | — | **header sidecar** |

v5 carries 13% fewer bytes per frame than v4 and is the first version that
actually round-trips through the service. `PROFILE_DENSE` carries 128,880 B per
frame — 2.0x v4 — at roughly twice the uploaded size.

## License

Copyright (C) 2026 DaniDevML

This program is free software: you can redistribute it and/or modify it under
the terms of the **GNU General Public License version 3** as published by the
Free Software Foundation, either version 3 of the License, or (at your option)
any later version. See [LICENSE](LICENSE) for the full text.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.

> Relicensed from MIT. Note that GPL-3.0 is copyleft: anyone distributing a
> modified version must release their changes under the same licence. If you
> also want that obligation to apply to people who run a modified version as a
> network service without distributing it, use the GNU **Affero** GPL (AGPL-3.0)
> instead — it is a drop-in swap of this file.
