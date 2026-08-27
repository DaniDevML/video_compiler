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
 * v5 changes over v4:
 *   • Packed-bit entry points. The caller passes the payload as packed bytes
 *     instead of one byte per bit, which removes an 8x intermediate array
 *     (75 MB for an 8 MB payload) from the hot path.
 *   • Multithreaded across frames via OpenMP when the compiler supports it.
 *   • Row-replication with 64-bit stores instead of byte-at-a-time fills:
 *     one pixel row per block row is built, then copied block_size times.
 *   • Any bits-per-block from 1 to 8, using arithmetic level mapping rather
 *     than hardcoded threshold ladders.
 *
 * Build:
 *   python native/build.py
 *
 * API:
 *   encode_plane / decode_plane                 — one byte per bit (v4 compat)
 *   encode_plane_packed / decode_plane_packed   — packed bits (v5 fast path)
 *   check_sync / check_sync_batch               — checkerboard sync scoring
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
  /* size_t lives in stddef.h, not stdint.h. The Windows branch above declares
     it by hand, which hid this: the file only ever failed to compile on a
     toolchain that did not, i.e. every non-Windows build. */
  #include <stddef.h>
#endif

#ifdef _OPENMP
  #include <omp.h>
  #define OMP_FOR(nt) _Pragma("omp parallel for schedule(static)")
#else
  #define OMP_FOR(nt)
#endif

/* ─── wide memory primitives ──────────────────────────────────────────────────
 * Hand-rolled so the library links against nothing at all. Unaligned 64-bit
 * accesses are fine on every architecture this targets.
 */
static void fill8(uint8_t *dst, uint8_t v, size_t n) {
    uint64_t w = 0x0101010101010101ULL * (uint64_t)v;
    while (n >= 8) { *(uint64_t *)dst = w; dst += 8; n -= 8; }
    while (n--) *dst++ = v;
}

static void copy8(uint8_t *dst, const uint8_t *src, size_t n) {
    while (n >= 8) {
        *(uint64_t *)dst = *(const uint64_t *)src;
        dst += 8; src += 8; n -= 8;
    }
    while (n--) *dst++ = *src++;
}

/* ─── level mapping ───────────────────────────────────────────────────────────
 * With L = 2^bpp levels the constellation is evenly spaced across 0..255:
 *     level[s] = round(s * 255 / (L-1))
 * and the matching nearest-level slicer is
 *     sym     = round(mean * (L-1) / 255)
 * For bpp=3 this reproduces {0,36,73,109,146,182,219,255} exactly, matching
 * the v4 hardcoded table, so old videos still decode bit-identically.
 */
static void build_levels(int bpp, uint8_t *lv) {
    int L = 1 << bpp;
    if (L == 1) { lv[0] = 0; return; }
    for (int s = 0; s < L; s++)
        lv[s] = (uint8_t)((s * 255 * 2 + (L - 1)) / (2 * (L - 1)));
}

static inline int slice_level(int mean, int Lm1) {
    return (mean * Lm1 + 127) / 255;
}

/* ─── packed bit access ───────────────────────────────────────────────────────
 * Bits are MSB-first within each byte, matching numpy's packbits/unpackbits.
 * get_bits reads up to 8 bits starting at an arbitrary bit position; the caller
 * guarantees at least 4 bytes of readable padding past the end of the payload.
 */
static inline uint32_t get_bits(const uint8_t *src, uint64_t pos, int n) {
    uint64_t byte = pos >> 3;
    int      off  = (int)(pos & 7);
    uint32_t v = ((uint32_t)src[byte] << 16)
               | ((uint32_t)src[byte + 1] << 8)
               |  (uint32_t)src[byte + 2];
    return (v >> (24 - off - n)) & ((1u << n) - 1);
}

/* OR n bits of value into the packed destination at bit position pos.
 * The destination must start zeroed. Callers keep per-frame bit ranges
 * byte-aligned so parallel frames never touch the same byte.
 */
static inline void put_bits(uint8_t *dst, uint64_t pos, int n, uint32_t val) {
    uint64_t byte = pos >> 3;
    int      off  = (int)(pos & 7);
    int      shift = 24 - off - n;
    uint32_t v = (val & ((1u << n) - 1)) << shift;
    dst[byte]     |= (uint8_t)(v >> 16);
    dst[byte + 1] |= (uint8_t)(v >> 8);
    dst[byte + 2] |= (uint8_t)(v);
}

/* ─── wide packed bit access (v8) ─────────────────────────────────────────────
 * get_bits/put_bits above read three bytes, so with a bit offset of up to 7
 * they can only move 17 bits. Base-3 luma packs 12 digits into 19 bits, which
 * does not fit. These 64-bit variants touch eight bytes and are safe for any
 * width up to 57 bits at any offset. Callers guarantee 8 bytes of readable
 * padding past the payload, as the narrow versions already required 4.
 */
static inline uint64_t get_bits64(const uint8_t *src, uint64_t pos, int n) {
    uint64_t byte = pos >> 3;
    int      off  = (int)(pos & 7);
    uint64_t v = 0;
    for (int i = 0; i < 8; i++) v = (v << 8) | (uint64_t)src[byte + i];
    return (v >> (64 - off - n)) & ((n == 64) ? ~0ULL : ((1ULL << n) - 1));
}

static inline void put_bits64(uint8_t *dst, uint64_t pos, int n, uint64_t val) {
    uint64_t byte = pos >> 3;
    int      off  = (int)(pos & 7);
    uint64_t mask = (n == 64) ? ~0ULL : ((1ULL << n) - 1);
    uint64_t v = (val & mask) << (64 - off - n);
    for (int i = 0; i < 8; i++)
        dst[byte + i] |= (uint8_t)(v >> (56 - 8 * i));
}

/* ─── base-N levels ───────────────────────────────────────────────────────────
 * Level counts that are not powers of two cannot be described in bits per
 * block, which is why the format was stuck on 2, 4, 8 and 16 levels. Real
 * YouTube measurement put the luma cliff between 2 levels (clean) and 4
 * (4.5e-02, unusable), with 3 levels clean at 2.7e-06 -- a rung the old format
 * had no way to express.
 *
 * A group of `digits` symbols carries `gbits` bits, chosen so that
 * n_levels**digits >= 2**gbits with the least waste. For 3 levels that is
 * 12 digits per 19 bits: 3**12 = 531441 >= 2**19 = 524288, 99.9% of the
 * theoretical log2(3) per digit.
 *
 * Keeping the group small also bounds error amplification. One corrupted digit
 * changes the whole group integer, so it can damage every byte the group
 * spans; at 19 bits that is at most 3 bytes, well inside what RS(255,215)
 * repairs. A larger group would be marginally more efficient and considerably
 * more fragile.
 */
static void build_levels_n(int n_levels, uint8_t *lv) {
    if (n_levels <= 1) { lv[0] = 0; return; }
    for (int s = 0; s < n_levels; s++)
        lv[s] = (uint8_t)((s * 255 * 2 + (n_levels - 1)) / (2 * (n_levels - 1)));
}

static inline int slice_level_n(int mean, int n_levels) {
    return (mean * (n_levels - 1) + 127) / 255;
}

/* ─── sync rows ───────────────────────────────────────────────────────────── */

static void write_sync(uint8_t *frame, int plane_w, int block_size,
                       int blocks_x, int sync_rows) {
    for (int sy = 0; sy < sync_rows; sy++) {
        int base_row = sy * block_size;
        uint8_t *row0 = frame + (size_t)base_row * plane_w;
        for (int bx = 0; bx < blocks_x; bx++)
            fill8(row0 + bx * block_size, ((sy + bx) & 1) ? 0 : 255, block_size);
        for (int py = 1; py < block_size; py++)
            copy8(frame + (size_t)(base_row + py) * plane_w, row0, plane_w);
    }
}

/* ─── encode_plane_packed ─────────────────────────────────────────────────────
 *
 * Convert packed payload bytes directly into a raw pixel plane.
 *
 *   src         packed payload bytes (MSB-first bit order)
 *   bit_offset  bit position in src where this plane's frame-0 data starts
 *   bit_stride  bits to advance in src between consecutive frames
 *               (= total bits per frame across all planes; must be a
 *                multiple of 8 so parallel frames never share a byte)
 *   out         output pixel buffer, n_frames * plane_h * plane_w
 */
EXPORT void encode_plane_packed(
    const uint8_t *src,
    uint64_t       bit_offset,
    uint64_t       bit_stride,
    uint8_t       *out,
    int n_frames,
    int plane_h,
    int plane_w,
    int block_size,
    int blocks_x,
    int blocks_y_data,
    int sync_rows,
    int bpp,
    int n_threads
) {
    uint8_t lv[256];
    build_levels(bpp, lv);
    const int frame_pixels = plane_h * plane_w;

    OMP_FOR(n_threads)
    for (int f = 0; f < n_frames; f++) {
        uint8_t *frame = out + (size_t)f * frame_pixels;
        uint64_t pos   = bit_offset + (uint64_t)f * bit_stride;

        write_sync(frame, plane_w, block_size, blocks_x, sync_rows);

        for (int dy = 0; dy < blocks_y_data; dy++) {
            int base_row = (sync_rows + dy) * block_size;
            uint8_t *row0 = frame + (size_t)base_row * plane_w;

            /* Build one pixel row for this block row ... */
            for (int bx = 0; bx < blocks_x; bx++) {
                uint32_t sym = get_bits(src, pos, bpp);
                pos += bpp;
                fill8(row0 + bx * block_size, lv[sym], block_size);
            }
            /* ... then replicate it down the block. */
            for (int py = 1; py < block_size; py++)
                copy8(frame + (size_t)(base_row + py) * plane_w, row0, plane_w);
        }

        int used_rows = (sync_rows + blocks_y_data) * block_size;
        if (used_rows < plane_h)
            fill8(frame + (size_t)used_rows * plane_w, 0,
                  (size_t)(plane_h - used_rows) * plane_w);
    }
}

/* ─── decode_plane_packed ─────────────────────────────────────────────────────
 *
 * Extract packed bits from raw pixel frames. `out` must start zeroed.
 *
 *   margin   pixels skipped on each block edge when averaging, for robustness
 *            against H.264 deblocking bleed at block boundaries
 */
EXPORT void decode_plane_packed(
    const uint8_t *frames,
    uint8_t       *out,
    uint64_t       bit_offset,
    uint64_t       bit_stride,
    int n_frames,
    int plane_h,
    int plane_w,
    int block_size,
    int blocks_x,
    int blocks_y_data,
    int sync_rows,
    int bpp,
    int margin,
    int n_threads
) {
    /* A margin wide enough to consume the whole block would leave nothing to
     * average, so fall back to sampling the block whole. */
    if (block_size - 2 * margin < 1) margin = 0;
    const int inner        = block_size - 2 * margin;
    const int inner_pixels = inner * inner;
    const int frame_pixels = plane_h * plane_w;
    const int Lm1          = (1 << bpp) - 1;

    OMP_FOR(n_threads)
    for (int f = 0; f < n_frames; f++) {
        const uint8_t *frame = frames + (size_t)f * frame_pixels;
        uint64_t pos = bit_offset + (uint64_t)f * bit_stride;

        for (int dy = 0; dy < blocks_y_data; dy++) {
            int base_row = (sync_rows + dy) * block_size + margin;
            const uint8_t *rp = frame + (size_t)base_row * plane_w + margin;

            for (int bx = 0; bx < blocks_x; bx++) {
                const uint8_t *blk = rp + bx * block_size;
                int sum = 0;
                for (int py = 0; py < inner; py++) {
                    const uint8_t *r = blk + (size_t)py * plane_w;
                    for (int px = 0; px < inner; px++)
                        sum += r[px];
                }
                put_bits(out, pos, bpp, slice_level(sum / inner_pixels, Lm1));
                pos += bpp;
            }
        }
    }
}

/* ─── encode_plane (v4 compat: one byte per bit) ───────────────────────────── */

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
    uint8_t lv[256];
    build_levels(bpp, lv);
    const int bits_per_frame = blocks_y_data * blocks_x * bpp;
    const int frame_pixels   = plane_h * plane_w;

    OMP_FOR(0)
    for (int f = 0; f < n_frames; f++) {
        uint8_t       *frame = out  + (size_t)f * frame_pixels;
        const uint8_t *b     = bits + (size_t)f * bits_per_frame;

        write_sync(frame, plane_w, block_size, blocks_x, sync_rows);

        int bit_idx = 0;
        for (int dy = 0; dy < blocks_y_data; dy++) {
            int base_row = (sync_rows + dy) * block_size;
            uint8_t *row0 = frame + (size_t)base_row * plane_w;
            for (int bx = 0; bx < blocks_x; bx++) {
                int sym = 0;
                for (int k = 0; k < bpp; k++)
                    sym = (sym << 1) | (b[bit_idx++] & 1);
                fill8(row0 + bx * block_size, lv[sym], block_size);
            }
            for (int py = 1; py < block_size; py++)
                copy8(frame + (size_t)(base_row + py) * plane_w, row0, plane_w);
        }

        int used_rows = (sync_rows + blocks_y_data) * block_size;
        if (used_rows < plane_h)
            fill8(frame + (size_t)used_rows * plane_w, 0,
                  (size_t)(plane_h - used_rows) * plane_w);
    }
}

/* ─── decode_plane (v4 compat: one byte per bit) ───────────────────────────── */

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
    if (block_size - 2 * margin < 1) margin = 0;
    const int inner          = block_size - 2 * margin;
    const int inner_pixels   = inner * inner;
    const int bits_per_frame = blocks_y_data * blocks_x * bpp;
    const int frame_pixels   = plane_h * plane_w;
    const int Lm1            = (1 << bpp) - 1;

    OMP_FOR(0)
    for (int f = 0; f < n_frames; f++) {
        const uint8_t *frame = frames + (size_t)f * frame_pixels;
        uint8_t       *b     = out    + (size_t)f * bits_per_frame;

        int bit_idx = 0;
        for (int dy = 0; dy < blocks_y_data; dy++) {
            int base_row = (sync_rows + dy) * block_size + margin;
            const uint8_t *rp = frame + (size_t)base_row * plane_w + margin;
            for (int bx = 0; bx < blocks_x; bx++) {
                const uint8_t *blk = rp + bx * block_size;
                int sum = 0;
                for (int py = 0; py < inner; py++) {
                    const uint8_t *r = blk + (size_t)py * plane_w;
                    for (int px = 0; px < inner; px++)
                        sum += r[px];
                }
                int sym = slice_level(sum / inner_pixels, Lm1);
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
    if (block_size - 2 * margin < 1) margin = 0;
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
    OMP_FOR(0)
    for (int f = 0; f < n_frames; f++) {
        scores_out[f] = check_sync(
            frames + (size_t)f * frame_pixels,
            plane_h, plane_w,
            block_size, blocks_x, sync_rows, margin
        );
    }
}

/* Reports whether the build has OpenMP, so Python can log the real thread count. */
EXPORT int native_threads(void) {
#ifdef _OPENMP
    return omp_get_max_threads();
#else
    return 1;
#endif
}

/* ─── base-N plane codec (v8) ─────────────────────────────────────────────────
 *
 * Same block layout and sync rows as the packed-bit path; the only difference
 * is that a block carries one base-`n_levels` digit instead of `bpp` bits.
 *
 * Per frame the caller guarantees blocks_x * blocks_y_data is a whole number
 * of groups and that gbits * groups is a multiple of 8, so frames stay
 * byte-aligned and threads never share a byte. For the shipping v8 luma
 * profile that holds exactly: 960 * 538 = 516480 blocks = 43040 groups of 12,
 * and 43040 * 19 = 817760 bits = 102220 bytes.
 */
EXPORT void encode_plane_basen(
    const uint8_t *src,
    uint64_t       bit_offset,
    uint64_t       bit_stride,
    uint8_t       *out,
    int n_frames,
    int plane_h,
    int plane_w,
    int block_size,
    int blocks_x,
    int blocks_y_data,
    int sync_rows,
    int n_levels,
    int digits,
    int gbits,
    int n_threads
) {
    uint8_t lv[256];
    build_levels_n(n_levels, lv);
    const int frame_pixels = plane_h * plane_w;
    const int per_frame    = blocks_x * blocks_y_data;
    const int n_groups     = per_frame / digits;

    OMP_FOR(n_threads)
    for (int f = 0; f < n_frames; f++) {
        uint8_t *frame = out + (size_t)f * frame_pixels;
        uint64_t pos   = bit_offset + (uint64_t)f * bit_stride;

        write_sync(frame, plane_w, block_size, blocks_x, sync_rows);

        /* Expand this frame's groups into a digit run, then paint blocks. */
        int gi = 0;                 /* groups consumed          */
        int di = digits;            /* digits left in cur group */
        uint64_t cur = 0;

        for (int dy = 0; dy < blocks_y_data; dy++) {
            int base_row = (sync_rows + dy) * block_size;
            uint8_t *row0 = frame + (size_t)base_row * plane_w;

            for (int bx = 0; bx < blocks_x; bx++) {
                if (di == digits) {
                    cur = (gi < n_groups) ? get_bits64(src, pos, gbits) : 0;
                    pos += gbits;
                    gi++;
                    di = 0;
                }
                int d = (int)(cur % (unsigned)n_levels);
                cur /= (unsigned)n_levels;
                di++;
                fill8(row0 + bx * block_size, lv[d], block_size);
            }
            for (int py = 1; py < block_size; py++)
                copy8(frame + (size_t)(base_row + py) * plane_w, row0, plane_w);
        }

        int used_rows = (sync_rows + blocks_y_data) * block_size;
        if (used_rows < plane_h)
            fill8(frame + (size_t)used_rows * plane_w, 0,
                  (size_t)(plane_h - used_rows) * plane_w);
    }
}

/* `out` must start zeroed: groups are OR-ed in. */
EXPORT void decode_plane_basen(
    const uint8_t *frames,
    uint8_t       *out,
    uint64_t       bit_offset,
    uint64_t       bit_stride,
    int n_frames,
    int plane_h,
    int plane_w,
    int block_size,
    int blocks_x,
    int blocks_y_data,
    int sync_rows,
    int n_levels,
    int digits,
    int gbits,
    int margin,
    int n_threads
) {
    const int frame_pixels = plane_h * plane_w;
    const int inner        = block_size - 2 * margin;
    const int inner_pixels = inner * inner;
    const int per_frame    = blocks_x * blocks_y_data;
    const int n_groups     = per_frame / digits;

    OMP_FOR(n_threads)
    for (int f = 0; f < n_frames; f++) {
        const uint8_t *frame = frames + (size_t)f * frame_pixels;
        uint64_t pos = bit_offset + (uint64_t)f * bit_stride;

        int gi = 0, di = 0;
        uint64_t cur = 0, place = 1;

        for (int dy = 0; dy < blocks_y_data; dy++) {
            int base_row = (sync_rows + dy) * block_size + margin;

            for (int bx = 0; bx < blocks_x; bx++) {
                int col = bx * block_size + margin;
                int sum = 0;
                for (int py = 0; py < inner; py++) {
                    const uint8_t *r = frame + (size_t)(base_row + py) * plane_w + col;
                    for (int px = 0; px < inner; px++) sum += r[px];
                }
                int d = slice_level_n(sum / inner_pixels, n_levels);

                cur += (uint64_t)d * place;
                place *= (unsigned)n_levels;
                di++;

                if (di == digits) {
                    if (gi < n_groups) put_bits64(out, pos, gbits, cur);
                    pos += gbits;
                    gi++;
                    di = 0;
                    cur = 0;
                    place = 1;
                }
            }
        }
    }
}
