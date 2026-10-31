/*
 * TMS5220 synthesis core: C reference for tms_speak.py synthesize().
 * Integer only; written for 16-bit int targets (AVR). Produces output
 * identical to the Python core. See docs/TMS5220_MODEL.md.
 *
 * Tables and behavior are from MAME src/devices/sound/tms5220.cpp and
 * tms5110r.hxx (BSD-3-Clause).
 */

#include <stdint.h>
#include <string.h>

static const uint8_t ENERGY[16] = {
    0, 1, 2, 3, 4, 6, 8, 11, 16, 23, 33, 47, 63, 85, 114, 0
};

static const uint8_t PERIOD[64] = {
      0,  15,  16,  17,  18,  19,  20,  21,  22,  23,  24,  25,  26,  27,  28,  29,
     30,  31,  32,  33,  34,  35,  36,  37,  38,  39,  40,  41,  42,  44,  46,  48,
     50,  52,  53,  56,  58,  60,  62,  65,  68,  70,  72,  76,  78,  80,  84,  86,
     91,  94,  98, 101, 105, 109, 114, 118, 122, 127, 132, 137, 142, 148, 153, 159
};

static const int16_t K1[32] = {
    -501, -498, -497, -495, -493, -491, -488, -482, -478, -474, -469, -464,
    -459, -452, -445, -437, -412, -380, -339, -288, -227, -158,  -81,   -1,
      80,  157,  226,  287,  337,  379,  411,  436
};
static const int16_t K2[32] = {
    -328, -303, -274, -244, -211, -175, -138,  -99,  -59,  -18,   24,   64,
     105,  143,  180,  215,  248,  278,  306,  331,  354,  374,  392,  408,
     422,  435,  445,  455,  463,  470,  476,  506
};
static const int16_t K3[16] = {
    -441, -387, -333, -279, -225, -171, -117,  -63,   -9,   45,   98,  152,  206,  260,  314,  368
};
static const int16_t K4[16] = {
    -328, -273, -217, -161, -106,  -50,    5,   61,  116,  172,  228,  283,  339,  394,  450,  506
};
static const int16_t K5[16] = {
    -328, -282, -235, -189, -142,  -96,  -50,   -3,   43,   90,  136,  182,  229,  275,  322,  368
};
static const int16_t K6[16] = {
    -256, -212, -168, -123,  -79,  -35,   10,   54,   98,  143,  187,  232,  276,  320,  365,  409
};
static const int16_t K7[16] = {
    -308, -260, -212, -164, -117,  -69,  -21,   27,   75,  122,  170,  218,  266,  314,  361,  409
};
static const int16_t K8[8]  = { -256, -161,  -66,   29,  124,  219,  314,  409 };
static const int16_t K9[8]  = { -256, -176,  -96,  -15,   65,  146,  226,  307 };
static const int16_t K10[8] = { -205, -132,  -59,   14,   87,  160,  234,  307 };

static const int16_t *const K[10] = { K1, K2, K3, K4, K5, K6, K7, K8, K9, K10 };

static const uint8_t K_BITS[10] = { 5, 5, 4, 4, 4, 4, 4, 3, 3, 3 };

/* Entries 21-51 are zero; the chirp address stops at 51. */
static const uint8_t CHIRP[21] = {
    0x00, 0x03, 0x0F, 0x28, 0x4C, 0x6C, 0x71, 0x50,
    0x25, 0x26, 0x4C, 0x44, 0x1A, 0x32, 0x3B, 0x13,
    0x37, 0x1A, 0x25, 0x1F, 0x1D
};

static const uint8_t INTERP_SHIFT[8] = { 0, 3, 3, 3, 2, 2, 1, 1 };

typedef struct {
    const uint8_t *data;
    uint16_t len;
    uint16_t byte_pos;
    uint8_t bit_pos;

    uint8_t e_idx, p_idx, k_idx[10];
    int16_t energy, prev_energy, pitch, k[10];
    uint8_t zpar, uv_zpar, old_e, old_p, inhibit, pitch_zero, talk, done;

    uint8_t ip, pc, subcycle;
    uint16_t pitch_count;
    uint16_t rng;

    int16_t u[10], x[10];
} tms5220_t;

static int16_t wrap14(int16_t v)
{
    return (int16_t)(((uint16_t)v + 0x4000u) & 0x7FFFu) - 0x4000;
}

static int16_t mul(int16_t k, int16_t v)
{
    return (int16_t)(((int32_t)k * wrap14(v)) >> 9);
}

static uint8_t read_bits(tms5220_t *s, uint8_t n)
{
    uint8_t v = 0;
    while (n--) {
        v <<= 1;
        if (s->byte_pos < s->len) {
            v |= (s->data[s->byte_pos] >> s->bit_pos) & 1;
            if (++s->bit_pos == 8) {
                s->bit_pos = 0;
                s->byte_pos++;
            }
        }
    }
    return v;
}

void tms5220_start(tms5220_t *s, const uint8_t *data, uint16_t len)
{
    static const uint8_t k_idle[10] = { 0, 0, 0, 0, 0xF, 0xF, 0xF, 0x7, 0x7, 0x7 };

    memset(s, 0, sizeof *s);
    s->data = data;
    s->len = len;
    memcpy(s->k_idx, k_idle, sizeof k_idle);
    s->zpar = s->uv_zpar = s->old_e = s->old_p = s->inhibit = s->talk = 1;
    s->subcycle = 1;
    s->rng = 0x1FFF;
}

/* Writes one 8-bit DAC sample (-128..127). Returns 0 when speech has ended. */
uint8_t tms5220_sample(tms5220_t *s, int8_t *dac)
{
    int8_t i;
    int8_t exc;
    int16_t y, out;

    if (s->done)
        return 0;

    if (s->ip == 0 && s->pc == 12 && s->subcycle == 1) {
        uint8_t new_unvoiced, new_silence;

        s->zpar = s->uv_zpar = 0;
        s->e_idx = (s->byte_pos >= s->len) ? 15 : read_bits(s, 4);
        if (s->e_idx == 15) {
            s->talk = 0;
        } else if (s->e_idx != 0) {
            uint8_t repeat = read_bits(s, 1);
            s->p_idx = read_bits(s, 6);
            s->uv_zpar = (s->p_idx == 0);
            if (!repeat) {
                for (i = 0; i < 4; i++)
                    s->k_idx[i] = read_bits(s, K_BITS[i]);
                if (s->p_idx != 0)
                    for (i = 4; i < 10; i++)
                        s->k_idx[i] = read_bits(s, K_BITS[i]);
            }
        }

        new_unvoiced = (s->p_idx == 0);
        new_silence = (s->e_idx == 0);
        s->inhibit = (!s->old_p && new_unvoiced) || (s->old_p && !new_unvoiced)
                  || (s->old_e && !new_silence) || (s->old_p && new_silence);
    } else if (s->subcycle == 2) {
        uint8_t shift = INTERP_SHIFT[s->ip];
        uint8_t hold = s->inhibit && s->ip != 0;

        if (s->pc == 0) {
            if (s->ip == 0)
                s->pitch_zero = 0;
            s->energy = s->zpar ? 0
                : s->energy + (hold ? 0 : (int16_t)(ENERGY[s->e_idx] - s->energy) >> shift);
        } else if (s->pc == 1) {
            s->pitch = s->zpar ? 0
                : s->pitch + (hold ? 0 : (int16_t)(PERIOD[s->p_idx] - s->pitch) >> shift);
        } else {
            uint8_t n = s->pc - 2;
            uint8_t zero = (n < 4) ? s->zpar : s->uv_zpar;
            s->k[n] = zero ? 0
                : s->k[n] + (hold ? 0 : (int16_t)(K[n][s->k_idx[n]] - s->k[n]) >> shift);
        }
    }

    if (s->old_p)
        exc = (s->rng & 1) ? -64 : 64;
    else
        exc = (s->pitch_count < 21) ? (int8_t)CHIRP[s->pitch_count] : 0;

    for (i = 0; i < 20; i++) {
        uint16_t bit = ((s->rng >> 12) ^ (s->rng >> 3) ^ (s->rng >> 2) ^ s->rng) & 1;
        s->rng = ((s->rng << 1) | bit) & 0x1FFF;
    }

    y = (int16_t)(s->prev_energy * exc) >> 3;
    for (i = 9; i >= 0; i--) {
        y = (int16_t)((uint16_t)y - (uint16_t)mul(s->k[i], s->x[i]));
        s->u[i] = y;
    }
    for (i = 9; i > 0; i--)
        s->x[i] = (int16_t)((uint16_t)s->x[i - 1] + (uint16_t)mul(s->k[i - 1], s->u[i - 1]));
    s->x[0] = s->u[0];
    s->prev_energy = s->energy;

    out = wrap14(s->u[0]);
    if (out > 2047)
        out = 2047;
    else if (out < -2048)
        out = -2048;
    *dac = (int8_t)(out >> 4);

    s->subcycle++;
    if (s->subcycle == 2 && s->pc == 12) {
        if (s->ip == 7) {
            if (s->inhibit)
                s->pitch_zero = 1;
            s->old_e = (s->e_idx == 0);
            s->old_p = (s->p_idx == 0);
            if (!s->talk) {
                s->done = 1;
                return 1;
            }
        }
        s->subcycle = 1;
        s->pc = 0;
        s->ip = (s->ip + 1) & 7;
    } else if (s->subcycle == 3) {
        s->subcycle = 1;
        s->pc++;
    }

    s->pitch_count++;
    if (s->pitch_count >= (uint16_t)s->pitch || s->pitch_zero)
        s->pitch_count = 0;
    s->pitch_count &= 0x1FF;

    return 1;
}
