/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "color.h"

#include <stddef.h>

static int32_t decode_signed_16bit_value(uint32_t value)
{
    value &= UINT32_C(0xffff);
    return value < UINT32_C(0x8000) ? (int32_t)value : (int32_t)value - 65536;
}

int bh7_color_weights(const uint8_t fraction[3], const uint8_t *rounding,
                     Bh7ColorWeights *weights)
{
    if (!fraction || !rounding || !weights)
        return -1;
    for (size_t channel = 0; channel < 3; ++channel) {
        if (fraction[channel] > 16)
            return -1;
    }

    /* The cube is split into six tetrahedra along its neutral diagonal. */
    uint8_t position[3] = {fraction[0], fraction[1], fraction[2]};
    uint8_t axis[3] = {4, 2, 1};
    for (size_t left = 0; left < 2; ++left) {
        for (size_t right = left + 1; right < 3; ++right) {
            if (position[right] > position[left]) {
                uint8_t temporary = position[left];
                position[left] = position[right];
                position[right] = temporary;
                temporary = axis[left];
                axis[left] = axis[right];
                axis[right] = temporary;
            }
        }
    }

    for (size_t corner = 0; corner < 8; ++corner)
        weights->corner[corner] = 0;
    weights->corner[0] = (uint16_t)((16u - position[0]) * 8192u / 3u);
    weights->corner[axis[0]] = (uint16_t)((position[0] - position[1]) * 8192u / 3u);
    weights->corner[axis[0] | axis[1]] = (uint16_t)((position[1] - position[2]) * 8192u / 3u);
    weights->corner[7] = (uint16_t)(position[2] * 8192u / 3u);

    size_t index = ((size_t)fraction[2] * 17u + fraction[1]) * 17u + fraction[0];
    uint8_t mask = rounding[index];
    uint32_t denominator = 0;
    for (size_t corner = 0; corner < 8; ++corner) {
        if (mask & (1u << corner)) {
            if (!weights->corner[corner])
                return -1;
            --weights->corner[corner];
        }
        denominator += weights->corner[corner];
    }
    if (!denominator)
        return -1;
    weights->denominator = (uint16_t)denominator;
    return 0;
}

int bh7_color_convert_pixel(const Bh7ColorProfile *profile, const uint8_t rgb[3],
                            uint16_t output[4])
{
    if (!profile || !rgb || !output)
        return -1;
    uint16_t component[3];
    if (profile->monochrome) {
        uint32_t luminance = ((uint32_t)rgb[0] * 30u +
                              (uint32_t)rgb[1] * 59u +
                              (uint32_t)rgb[2] * 11u) / 100u;
        if (profile->direct_monochrome) {
            if (!profile->monochrome_curve)
                return -1;
            uint16_t value = luminance == 255u ? 0 : profile->monochrome_curve[255u - luminance];
            for (size_t channel = 0; channel < 4; ++channel)
                output[channel] = value;
            return 0;
        }
        for (size_t channel = 0; channel < 3; ++channel)
            component[channel] = (uint16_t)luminance;
    } else {
        bool white = rgb[0] == 255u && rgb[1] == 255u && rgb[2] == 255u;
        for (size_t channel = 0; channel < 3; ++channel)
            component[channel] = white || !profile->rgb_curves
                ? rgb[channel] : profile->rgb_curves[channel][rgb[channel]];
    }

    if (!profile->cube)
        return -1;
    uint32_t sum = (uint32_t)component[0] + component[1] + component[2];
    if (!sum || sum == 765u) {
        size_t node = sum ? BH7_COLOR_CUBE_NODES - 1u : 0u;
        for (size_t channel = 0; channel < 4; ++channel)
            output[channel] = profile->cube[node * 4u + channel];
        return 0;
    }

    uint8_t fraction[3];
    for (size_t channel = 0; channel < 3; ++channel) {
        fraction[channel] = (uint8_t)(component[channel] & 15u);
        if (component[channel] == 255u)
            ++fraction[channel];
    }
    Bh7ColorWeights weights;
    if (bh7_color_weights(fraction, profile->weight_rounding, &weights))
        return -1;

    size_t node = (((size_t)component[0] >> 4) * 17u +
                   ((size_t)component[1] >> 4)) * 17u +
                   ((size_t)component[2] >> 4);
    uint32_t accumulated[4] = {0, 0, 0, 0};
    for (size_t corner = 0; corner < 8; ++corner) {
        uint32_t weight = weights.corner[corner];
        if (!weight)
            continue;
        size_t offset = ((corner & 4u) ? 289u : 0u) +
                        ((corner & 2u) ? 17u : 0u) +
                        ((corner & 1u) ? 1u : 0u);
        const uint16_t *sample = profile->cube + (node + offset) * 4u;
        for (size_t channel = 0; channel < 4; ++channel)
            accumulated[channel] += (uint32_t)sample[channel] * weight;
    }
    for (size_t channel = 0; channel < 4; ++channel)
        output[channel] = (uint16_t)(accumulated[channel] / weights.denominator);

    if (profile->neutral_balance && component[0] == component[1] &&
        component[1] == component[2]) {
        output[1] = output[0];
        output[2] = output[0];
        if (output[3] < output[0])
            output[3] = output[0];
    }
    return 0;
}

static int curve_input_is_valid(const Bh7ColorFinishProfile *profile,
                             const uint16_t input[4], uint16_t output[4])
{
    if (!profile || !profile->tone_curves || !input || !output)
        return 0;
    for (size_t channel = 0; channel < 4; ++channel) {
        if (input[channel] >= BH7_COLOR_CURVE_LENGTH)
            return 0;
    }
    return 1;
}

int bh7_color_finish_additive(const Bh7ColorFinishProfile *profile,
                             const uint16_t input[4], uint16_t output[4])
{
    if (!curve_input_is_valid(profile, input, output) || !profile->black_correction)
        return -1;
    uint16_t mapped[4];
    for (size_t channel = 0; channel < 4; ++channel) {
        /* Correction addition wraps to the original signed 16-bit domain. */
        int32_t corrected = decode_signed_16bit_value((uint16_t)((int32_t)input[channel] +
                        profile->black_correction[channel][input[3]]));
        if (corrected < 0)
            corrected = 0;
        else if (corrected > 1023)
            corrected = 1023;
        mapped[channel] = profile->tone_curves[channel][corrected];
    }
    uint32_t total = (uint32_t)mapped[0] + mapped[1] + mapped[2] + mapped[3];
    if (profile->ink_limit < (uint16_t)total) {
        if (decode_signed_16bit_value(mapped[3]) < decode_signed_16bit_value(profile->ink_limit)) {
            uint32_t denominator = (uint32_t)(uint16_t)total - mapped[3];
            if (!denominator)
                return -1;
            uint32_t scale = (((uint32_t)profile->ink_limit - mapped[3]) * 256u) / denominator;
            for (size_t channel = 0; channel < 3; ++channel)
                mapped[channel] = (uint16_t)(((uint32_t)mapped[channel] * scale) >> 8);
        } else {
            mapped[0] = mapped[1] = mapped[2] = 0;
            mapped[3] = profile->ink_limit;
        }
    }
    for (size_t channel = 0; channel < 4; ++channel)
        output[channel] = mapped[channel];
    return 0;
}

int bh7_color_finish_shared_black(const Bh7ColorFinishProfile *profile,
                                 const uint16_t input[4], uint16_t output[4])
{
    if (!curve_input_is_valid(profile, input, output))
        return -1;
    uint16_t black = profile->tone_curves[3][input[3]];
    uint16_t separated[3];
    for (size_t channel = 0; channel < 3; ++channel) {
        uint16_t value = profile->tone_curves[channel][input[channel]];
        if (decode_signed_16bit_value(value) < decode_signed_16bit_value(black))
            value = black;
        separated[channel] = (uint16_t)(value - black);
    }
    uint32_t total = (uint32_t)separated[0] + separated[1] + separated[2] + black;
    if (profile->ink_limit < (uint16_t)total) {
        if (decode_signed_16bit_value(black) < decode_signed_16bit_value(profile->ink_limit)) {
            uint32_t denominator = (uint32_t)(uint16_t)total - black;
            if (!denominator)
                return -1;
            uint32_t scale = (((uint32_t)profile->ink_limit - black) * 256u) / denominator;
            for (size_t channel = 0; channel < 3; ++channel)
                separated[channel] = (uint16_t)((((uint32_t)separated[channel] * scale) >> 8) + black);
        } else {
            separated[0] = separated[1] = separated[2] = 0;
            black = profile->ink_limit;
        }
    } else {
        for (size_t channel = 0; channel < 3; ++channel)
            separated[channel] = (uint16_t)(separated[channel] + black);
    }
    for (size_t channel = 0; channel < 3; ++channel)
        output[channel] = separated[channel];
    output[3] = black;
    return 0;
}
