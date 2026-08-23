/*
 * rs.c — Reed-Solomon decoder over GF(2^8) for the RS(255, k) codes this
 * project uses.
 *
 * Why this exists:
 *   Decoding, not encoding, dominates the pipeline at scale. Off YouTube the
 *   payload always carries a few errors, so the cheap path (strip parity,
 *   check CRC) never fires on a large archive and every chunk goes through
 *   full correction. The galois/numba decoder costs roughly 9 ms per damaged
 *   255-byte chunk, which works out to minutes per hundred megabytes.
 *
 *   The algorithm itself is not expensive: syndromes, Berlekamp-Massey, Chien
 *   search and Forney over a 255-symbol codeword is microseconds of work with
 *   table-driven field arithmetic.
 *
 * Compatibility:
 *   Must match galois.ReedSolomon(255, k) exactly, since existing videos were
 *   encoded with it. That means GF(2^8) with the primitive polynomial
 *   x^8+x^4+x^3+x^2+1 (0x11D), primitive element alpha = 2, generator roots
 *   starting at alpha^1 (galois's default c=1), and a systematic codeword with
 *   the message first and parity last. bench/test_rs_native.py checks this
 *   against galois over randomised error patterns.
 *
 * Codeword layout: cw[0..n-1], where cw[i] is the coefficient of x^(n-1-i).
 */

#ifdef _WIN32
  #define EXPORT __declspec(dllexport)
  typedef unsigned char      uint8_t;
  typedef unsigned short     uint16_t;
  typedef unsigned int       uint32_t;
  typedef unsigned long long size_t;
#else
  #define EXPORT __attribute__((visibility("default")))
  #include <stdint.h>
  #include <stddef.h>
#endif

#ifdef _OPENMP
  #include <omp.h>
#endif

#define GF_POLY 0x11D
#define MAXN    255
#define MAXROOTS 64

static uint8_t gf_exp[512];
static uint8_t gf_log[256];
static int     gf_ready = 0;

static void gf_init(void) {
    if (gf_ready) return;
    int x = 1;
    for (int i = 0; i < 255; i++) {
        gf_exp[i] = (uint8_t)x;
        gf_log[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100) x ^= GF_POLY;
    }
    /* Duplicate so index arithmetic up to 510 needs no modulo. */
    for (int i = 255; i < 512; i++) gf_exp[i] = gf_exp[i - 255];
    gf_log[0] = 0;      /* never used; log(0) is undefined */
    gf_ready = 1;
}

static inline uint8_t gf_mul(uint8_t a, uint8_t b) {
    if (a == 0 || b == 0) return 0;
    return gf_exp[gf_log[a] + gf_log[b]];
}

static inline uint8_t gf_inv(uint8_t a) {
    return gf_exp[255 - gf_log[a]];
}

static inline uint8_t gf_div(uint8_t a, uint8_t b) {
    if (a == 0) return 0;
    return gf_exp[gf_log[a] + 255 - gf_log[b]];
}

static inline uint8_t gf_pow(uint8_t a, int n) {
    if (a == 0) return 0;
    int e = (gf_log[a] * n) % 255;
    if (e < 0) e += 255;
    return gf_exp[e];
}

/* ─── decode one codeword in place ────────────────────────────────────────────
 *
 * Returns the number of symbols corrected, or -1 if the codeword is beyond the
 * code's correction capability. A -1 return leaves the codeword untouched.
 */
static int rs_decode_one(uint8_t *cw, int n, int nroots) {
    const int t = nroots / 2;
    uint8_t synd[MAXROOTS];
    int any = 0;

    /* ── Syndromes: S_j = r(alpha^(1+j)) ── */
    for (int j = 0; j < nroots; j++) {
        uint8_t root = gf_exp[(j + 1) % 255];
        uint8_t acc = 0;
        for (int i = 0; i < n; i++)
            acc = gf_mul(acc, root) ^ cw[i];   /* Horner over x^(n-1-i) */
        synd[j] = acc;
        if (acc) any = 1;
    }
    if (!any) return 0;                        /* clean codeword */

    /* ── Berlekamp-Massey: find the error locator sigma(x) ──
     * Fixed-length polynomials over the whole working degree, so no term can
     * be dropped by a length bookkeeping slip.
     */
    const int SLEN = MAXROOTS + 2;
    uint8_t sigma[MAXROOTS + 2], prev[MAXROOTS + 2], tmp[MAXROOTS + 2];
    for (int i = 0; i < SLEN; i++) { sigma[i] = 0; prev[i] = 0; }
    sigma[0] = 1;
    prev[0] = 1;
    int L = 0, m = 1;
    uint8_t b = 1;

    for (int r = 0; r < nroots; r++) {
        uint8_t delta = synd[r];
        for (int i = 1; i <= L; i++)
            delta ^= gf_mul(sigma[i], synd[r - i]);

        if (delta == 0) {
            m++;
        } else {
            for (int i = 0; i < SLEN; i++) tmp[i] = sigma[i];

            uint8_t coef = gf_div(delta, b);
            for (int i = 0; i + m < SLEN; i++)
                sigma[i + m] ^= gf_mul(coef, prev[i]);

            if (2 * L <= r) {
                L = r + 1 - L;
                for (int i = 0; i < SLEN; i++) prev[i] = tmp[i];
                b = delta;
                m = 1;
            } else {
                m++;
            }
        }
    }

    if (L > t) return -1;                      /* more errors than correctable */

    int sigma_len = L + 1;

    /* ── Chien search: roots of sigma give the error positions ──
     * Position i (array index) corresponds to x^(n-1-i); it is in error when
     * sigma(alpha^-(n-1-i)) == 0.
     */
    int pos[MAXROOTS];
    int nerr = 0;
    for (int i = 0; i < n; i++) {
        int exp_i = (n - 1 - i) % 255;
        uint8_t xinv = gf_exp[(255 - exp_i) % 255];
        uint8_t acc = 0;
        /* evaluate sigma at xinv */
        for (int k = sigma_len - 1; k >= 0; k--)
            acc = gf_mul(acc, xinv) ^ sigma[k];
        if (acc == 0) {
            if (nerr >= t) return -1;
            pos[nerr++] = i;
        }
    }
    if (nerr != L) return -1;                  /* locator degree must match */

    /* ── Forney: omega(x) = S(x) * sigma(x) mod x^nroots ── */
    uint8_t omega[MAXROOTS + 2];
    for (int i = 0; i < nroots; i++) {
        uint8_t acc = 0;
        for (int j = 0; j <= i && j < sigma_len; j++)
            acc ^= gf_mul(sigma[j], synd[i - j]);
        omega[i] = acc;
    }

    for (int e = 0; e < nerr; e++) {
        int exp_i = (n - 1 - pos[e]) % 255;
        uint8_t xinv = gf_exp[(255 - exp_i) % 255];   /* X_i^-1 */

        /* omega(xinv) */
        uint8_t num = 0;
        for (int k = nroots - 1; k >= 0; k--)
            num = gf_mul(num, xinv) ^ omega[k];

        /* Formal derivative sigma'(xinv). Over GF(2^m) the even-index terms
         * vanish, leaving sigma'(x) = sum_{k odd} sigma[k] x^(k-1). */
        uint8_t den = 0;
        for (int k = 1; k < sigma_len; k += 2)
            den ^= gf_mul(sigma[k], gf_pow(xinv, k - 1));
        if (den == 0) return -1;

        /* Forney: e_i = X_i^(1-c) * omega(X_i^-1) / sigma'(X_i^-1).
         * The generator roots start at alpha^1 (galois's default c=1), so the
         * X_i^(1-c) factor is X_i^0 = 1 and drops out. */
        cw[pos[e]] ^= gf_div(num, den);
    }

    /* ── Verify: syndromes must now be zero ── */
    for (int j = 0; j < nroots; j++) {
        uint8_t root = gf_exp[(j + 1) % 255];
        uint8_t acc = 0;
        for (int i = 0; i < n; i++)
            acc = gf_mul(acc, root) ^ cw[i];
        if (acc) return -1;
    }
    return nerr;
}

/* ─── rs_decode_batch ─────────────────────────────────────────────────────────
 *
 * Decode n_chunks consecutive codewords in place.
 *
 *   status_out[i]  number of symbols corrected in chunk i, or -1 if that chunk
 *                  could not be corrected. The caller decides what an
 *                  uncorrectable chunk means; nothing is raised here.
 *
 * Returns the number of chunks that failed.
 */
EXPORT int rs_decode_batch(
    uint8_t *data,
    int     *status_out,
    int      n_chunks,
    int      n,
    int      nroots,
    int      n_threads
) {
    gf_init();
    if (nroots > MAXROOTS || n > MAXN) return -1;

    int failed = 0;
#ifdef _OPENMP
    #pragma omp parallel for schedule(static) reduction(+:failed) \
            num_threads(n_threads > 0 ? n_threads : omp_get_max_threads())
#endif
    for (int c = 0; c < n_chunks; c++) {
        int r = rs_decode_one(data + (size_t)c * n, n, nroots);
        status_out[c] = r;
        if (r < 0) failed++;
    }
    return failed;
}

/* ─── generator polynomial ────────────────────────────────────────────────────
 *
 * g(x) = prod_{j=0}^{nroots-1} (x + alpha^(1+j)), monic, g[0] = 1 for x^nroots.
 */
static void rs_gen_poly(int nroots, uint8_t *g) {
    for (int i = 0; i <= nroots; i++) g[i] = 0;
    g[0] = 1;
    int deg = 0;
    for (int j = 0; j < nroots; j++) {
        uint8_t root = gf_exp[(j + 1) % 255];
        /* multiply g(x) by (x + root) */
        deg++;
        for (int i = deg; i > 0; i--)
            g[i] = g[i - 1] ^ gf_mul(g[i], root);
        g[0] = gf_mul(g[0], root);
    }
    /* The loop above builds the coefficients with g[deg] as the leading term;
     * reverse so g[0] is the x^nroots coefficient. */
    for (int i = 0, j = nroots; i < j; i++, j--) {
        uint8_t tmp = g[i]; g[i] = g[j]; g[j] = tmp;
    }
}

/* ─── rs_encode_batch ─────────────────────────────────────────────────────────
 *
 * Systematic encode: reads n_chunks messages of k symbols from `msg` and writes
 * complete n-symbol codewords (message then parity) to `out`.
 */
EXPORT int rs_encode_batch(
    const uint8_t *msg,
    uint8_t       *out,
    int            n_chunks,
    int            n,
    int            nroots,
    int            n_threads
) {
    gf_init();
    if (nroots > MAXROOTS || n > MAXN) return -1;
    const int k = n - nroots;

    uint8_t g[MAXROOTS + 1];
    rs_gen_poly(nroots, g);

#ifdef _OPENMP
    #pragma omp parallel for schedule(static) \
            num_threads(n_threads > 0 ? n_threads : omp_get_max_threads())
#endif
    for (int c = 0; c < n_chunks; c++) {
        const uint8_t *m = msg + (size_t)c * k;
        uint8_t *cw = out + (size_t)c * n;
        uint8_t par[MAXROOTS];
        for (int i = 0; i < nroots; i++) par[i] = 0;

        for (int i = 0; i < k; i++) {
            cw[i] = m[i];
            uint8_t fb = m[i] ^ par[0];
            for (int j = 0; j < nroots - 1; j++)
                par[j] = par[j + 1] ^ gf_mul(fb, g[j + 1]);
            par[nroots - 1] = gf_mul(fb, g[nroots]);
        }
        for (int i = 0; i < nroots; i++) cw[k + i] = par[i];
    }
    return 0;
}

/* Reports whether the RS decoder is present and how wide it can go. */
EXPORT int rs_max_roots(void) { return MAXROOTS; }
