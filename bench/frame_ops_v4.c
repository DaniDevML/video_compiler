/*
 * frame_ops.c — C implementation of the pixel-level encode/decode inner loops.
 *
 * Why C here:
 *   The encode/decode hot paths are simple nested loops over tens of millions
 *   of pixels per second.  NumPy is great at whole-array ops but pays Python
 *   dispatch overhead and unavoidable intermediate allocations.  This file
 *   compiles to a tiny shared library that Python loads via ctypes; each call
 *   skips the Python interpreter entirely for the inner loop.
 *
 * Build:
 *   python native/build.py
 *
 * Compile manually (Windows, MSVC):
 *   cl /O2 /LD frame_ops.c /Fe:frame_ops.dll
 *
 * Compile manually (Linux/macOS, GCC):
 *   gcc -O3 -march=native -shared -fPIC -o frame_ops.so frame_ops.c
 *
 * API:
 *   encode_plane  — bits (0/1 flat array) → grayscale pixel plane
 *   decode_plane  — grayscale pixel plane  → bits (0/1 flat array)
 *   check_sync    — score the checkerboard sync rows of one frame
 */

/* Self-contained: no system headers required (avoids Windows SDK dependency). */
#ifdef _WIN32
  #define EXPORT __declspec(dllexport)
  typedef unsigned char      uint8_t;
  typedef unsigned short     uint16_t;
  typedef unsigned int       uint32_t;
  typedef unsigned long long uint64_t;
  typedef long long          intptr_t;
  typedef unsigned long long size_t;
#else
  #define EXPORT __attribute__((visibility("default")))
  #include <stdint.h>
  #include <string.h>
#endif

/* Inline block-fill — avoids memset intrinsic conflict on MSVC. */
static void fill_bytes(uint8_t *dst, uint8_t val, size_t n) {
    while (n--) *dst++ = val;
}

/* Gray levels for each bpp mode.
 *
 *  bpp=1: 2 levels  {  0, 255 }                                — 1 bit/block
 *  bpp=2: 4 levels  {  0,  85, 170, 255 }                      — 2 bits/block
 *  bpp=3: 8 levels  {  0,  36,  73, 109, 146, 182, 219, 255 }  — 3 bits/block
 *
 * bpp=2 gap = 85, bpp=3 gap ≈ 36 — both above ±15 YouTube H.264 noise at 4×4 blocks.
 */
static const uint8_t kLevels1[2] = {0, 255};
static const uint8_t kLevels2[4] = {0, 85, 170, 255};
static const uint8_t kLevels3[8] = {0, 36, 73, 109, 146, 182, 219, 255};

/* Quantise a mean pixel value to a symbol index for a given bpp. */
static inline int quantize(int mean, int bpp) {
    if (bpp == 1) return mean >= 128 ? 1 : 0;
    if (bpp == 2) {
        /* thresholds at midpoints: 43, 128, 213 */
        if (mean < 43)  return 0;
        if (mean < 128) return 1;
        if (mean < 213) return 2;
        return 3;
    }
    /* bpp == 3: 8 levels, thresholds at midpoints between kLevels3 */
    if (mean <  18) return 0;
    if (mean <  55) return 1;
    if (mean <  91) return 2;
    if (mean < 128) return 3;
    if (mean < 164) return 4;
    if (mean < 200) return 5;
    if (mean < 237) return 6;
    return 7;
}

/* ─── encode_plane ────────────────────────────────────────────────────────────
 *
 * Convert a flat 0/1 bit array into a raw pixel plane (one video plane).
 *
 *   bits        flat uint8 array, length = n_frames * blocks_y_data * blocks_x * bpp
 *               Each byte is 0 or 1. Bits are stored MSB-first per block:
 *               for bpp=2 block (i):  bits[i*2+0] = MSB, bits[i*2+1] = LSB
 *
 *   out         output pixel buffer, length = n_frames * plane_h * plane_w
 *               Filled with: sync rows (checkerboard) then data rows.
 *
 *   n_frames, plane_h, plane_w   dimensions
 *   block_size                   pixel size of each encoded block
 *   blocks_x                     number of blocks horizontally
 *   blocks_y_data                number of data-block rows (not counting sync)
 *   sync_rows                    number of checkerboard sync rows at top
 *   bpp                          bits per block: 1 or 2
 */
EXPORT void encode_plane(
    const uint8_t *bits,
    uint8_t       *out,
    int n_frames,
    int plane_h,
    int plane_w,
    int block_size,
    int blocks_x,
    int blocks_y_data,
    int sync_rows,
    int bpp
) {
    const uint8_t *lvls      = (bpp == 1) ? kLevels1 : (bpp == 2) ? kLevels2 : kLevels3;
    int bits_per_block        = bpp;
    int bits_per_frame        = blocks_y_data * blocks_x * bits_per_block;
    int frame_pixels          = plane_h * plane_w;

    for (int f = 0; f < n_frames; f++) {
        uint8_t       *frame = out  + (size_t)f * frame_pixels;
        const uint8_t *b     = bits + (size_t)f * bits_per_frame;

        /* ── Sync rows: checkerboard (black/white regardless of bpp) ── */
        for (int sy = 0; sy < sync_rows; sy++) {
            for (int bx = 0; bx < blocks_x; bx++) {
                uint8_t pv = ((sy + bx) & 1) ? 0 : 255;
                int base_row = sy * block_size;
                for (int py = 0; py < block_size; py++) {
                    fill_bytes(frame + (base_row + py) * plane_w + bx * block_size,
                           pv, block_size);
                }
            }
        }

        /* ── Data rows ── */
        int bit_idx = 0;
        for (int dy = 0; dy < blocks_y_data; dy++) {
            int base_row = (sync_rows + dy) * block_size;
            for (int bx = 0; bx < blocks_x; bx++) {
                /* Read bpp bits MSB-first → symbol 0..2^bpp-1 */
                int sym = 0;
                for (int k = 0; k < bpp; k++)
                    sym = (sym << 1) | (b[bit_idx++] & 1);

                uint8_t pv = lvls[sym];
                for (int py = 0; py < block_size; py++) {
                    fill_bytes(frame + (base_row + py) * plane_w + bx * block_size,
                           pv, block_size);
                }
            }
        }

        /* ── Any trailing rows below data area → black ── */
        int used_rows = (sync_rows + blocks_y_data) * block_size;
        if (used_rows < plane_h)
            fill_bytes(frame + (size_t)used_rows * plane_w, 0,
                   (size_t)(plane_h - used_rows) * plane_w);
    }
}


/* ─── decode_plane ────────────────────────────────────────────────────────────
 *
 * Extract bits from raw pixel frames.
 *
 *   frames      input pixel buffer, length = n_frames * plane_h * plane_w
 *   out         output 0/1 bit array, length = n_frames * blocks_y_data * blocks_x * bpp
 *   margin      pixels to skip on each edge when computing block mean (robustness vs
 *               H.264 deblocking filter edge bleed)
 */
EXPORT void decode_plane(
    const uint8_t *frames,
    uint8_t       *out,
    int n_frames,
    int plane_h,
    int plane_w,
    int block_size,
    int blocks_x,
    int blocks_y_data,
    int sync_rows,
    int bpp,
    int margin
) {
    int inner            = block_size - 2 * margin;
    int inner_pixels     = inner * inner;
    int bits_per_frame   = blocks_y_data * blocks_x * bpp;
    int frame_pixels     = plane_h * plane_w;

    for (int f = 0; f < n_frames; f++) {
        const uint8_t *frame = frames + (size_t)f * frame_pixels;
        uint8_t       *b     = out    + (size_t)f * bits_per_frame;

        int bit_idx = 0;
        for (int dy = 0; dy < blocks_y_data; dy++) {
            int base_row = (sync_rows + dy) * block_size + margin;
            for (int bx = 0; bx < blocks_x; bx++) {
                int base_col = bx * block_size + margin;

                /* Compute mean of the inner (block_size-2m)×(block_size-2m) pixels */
                int sum = 0;
                for (int py = 0; py < inner; py++) {
                    const uint8_t *row = frame + (base_row + py) * plane_w + base_col;
                    for (int px = 0; px < inner; px++)
                        sum += row[px];
                }
                int mean = sum / inner_pixels;
                int sym  = quantize(mean, bpp);

                /* Unpack symbol to bpp bits, MSB first */
                for (int k = bpp - 1; k >= 0; k--)
                    b[bit_idx++] = (sym >> k) & 1;
            }
        }
    }
}


/* ─── check_sync (single frame) ───────────────────────────────────────────────
 *
 * Score how well the sync rows of one frame match the expected checkerboard.
 * Returns 0–100 (integer percentage of correctly-decoded sync blocks).
 */
EXPORT int check_sync(
    const uint8_t *frame,
    int plane_h,
    int plane_w,
    int block_size,
    int blocks_x,
    int sync_rows,
    int margin
) {
    int inner        = block_size - 2 * margin;
    int inner_pixels = inner * inner;
    int correct = 0, total = 0;

    for (int sy = 0; sy < sync_rows; sy++) {
        int base_row = sy * block_size + margin;
        for (int bx = 0; bx < blocks_x; bx++) {
            int base_col = bx * block_size + margin;
            int expected_white = ((sy + bx) & 1) ? 0 : 1;

            int sum = 0;
            for (int py = 0; py < inner; py++) {
                const uint8_t *row = frame + (base_row + py) * plane_w + base_col;
                for (int px = 0; px < inner; px++)
                    sum += row[px];
            }
            int got_white = (sum / inner_pixels) >= 128;
            if (got_white == expected_white) correct++;
            total++;
        }
    }
    return total ? correct * 100 / total : 0;
}


/* ─── check_sync_batch ────────────────────────────────────────────────────────
 *
 * Score multiple frames at once, writing results to an int array.
 * Avoids N separate Python→C round-trips.
 */
EXPORT void check_sync_batch(
    const uint8_t *frames,
    int           *scores_out,
    int n_frames,
    int plane_h,
    int plane_w,
    int block_size,
    int blocks_x,
    int sync_rows,
    int margin
) {
    int frame_pixels = plane_h * plane_w;
    for (int f = 0; f < n_frames; f++) {
        scores_out[f] = check_sync(
            frames + (size_t)f * frame_pixels,
            plane_h, plane_w,
            block_size, blocks_x, sync_rows, margin
        );
    }
}
