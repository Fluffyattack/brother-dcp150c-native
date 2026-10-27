/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef BH7_COLOR_H
#define BH7_COLOR_H

#include <stdbool.h>
#include <stdint.h>

#define BH7_COLOR_CUBE_EDGE 17u
#define BH7_COLOR_CUBE_NODES 4913u
#define BH7_COLOR_CUBE_WORDS (BH7_COLOR_CUBE_NODES * 4u)
#define BH7_COLOR_CURVE_LENGTH 1024u

/* Runtime profile data is supplied by the caller and retains its own license. */
typedef struct {
    const uint16_t *cube; /* Node order: red, green, blue; four output channels. */
    const uint8_t (*rgb_curves)[256]; /* NULL selects identity preprocessing. */
    const uint8_t *monochrome_curve; /* 256 entries indexed by inverse luminance. */
    const uint8_t *weight_rounding; /* 4913 corner-rounding masks, blue/green/red. */
    bool monochrome;
    bool direct_monochrome;
    bool neutral_balance;
} Bh7ColorProfile;

typedef struct {
    uint16_t denominator;
    /* Corner bits are red=4, green=2, blue=1. */
    uint16_t corner[8];
} Bh7ColorWeights;

typedef struct {
    const uint16_t (*tone_curves)[BH7_COLOR_CURVE_LENGTH];
    const int16_t (*black_correction)[BH7_COLOR_CURVE_LENGTH];
    uint16_t ink_limit;
} Bh7ColorFinishProfile;

/* Fractions include the endpoint 16. All functions return 0 or -1 on bad input. */
int bh7_color_weights(const uint8_t fraction[3], const uint8_t *rounding,
                     Bh7ColorWeights *weights);
int bh7_color_convert_pixel(const Bh7ColorProfile *profile, const uint8_t rgb[3],
                            uint16_t output[4]);

/* Input values must be valid 10-bit curve indices (0..1023). */
int bh7_color_finish_additive(const Bh7ColorFinishProfile *profile,
                             const uint16_t input[4], uint16_t output[4]);
int bh7_color_finish_shared_black(const Bh7ColorFinishProfile *profile,
                                 const uint16_t input[4], uint16_t output[4]);

#endif
