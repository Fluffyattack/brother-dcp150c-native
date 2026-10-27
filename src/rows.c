/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "rows.h"
#include <limits.h>
#include <stdlib.h>
#include <string.h>

struct Bh7Rows {
    Bh7RowsConfig config;
    Bh7RowsTables tables;
    Bh7ColorProfile color;
    Bh7ColorFinishProfile finish;
    const uint16_t *direct_black_tone;
    size_t axis_width;
    size_t error_count;
    int16_t *errors[4]; /* Black, cyan, magenta, yellow. */
    uint32_t random_index;
    uint32_t noise_index;
    uint32_t phase;
    uint32_t ordered_phase;
    bool monochrome_only;
};

static int32_t decode_signed_16bit_value(uint32_t value)
{
    value &= UINT32_C(0xffff);
    return value < UINT32_C(0x8000) ? (int32_t)value : (int32_t)value - 65536;
}

static int16_t wrap_to_signed_16bit(int32_t value)
{
    return (int16_t)decode_signed_16bit_value((uint32_t)value);
}

static int32_t divide_by_two_rounding_down(int32_t value)
{
    return value < 0 ? -((-value + 1) / 2) : value / 2;
}

static bool uses_alternate_plain_paper_profile(int16_t paper)
{
    return paper == 34 || paper == 28 || paper == 91 || paper == 73 ||
           paper == 30 || paper == 20 || paper == 27 || paper == 37;
}

int bh7_rows_configure(Bh7RowsConfig *config, uint16_t mode_word,
                       int16_t media, int16_t paper, bool ordered, bool rotated)
{
    if (!config)
        return -1;
    memset(config, 0, sizeof *config);
    uint32_t selector = mode_word & (ordered ? 0x0f0fu : 0x2f0fu);
    if (!ordered) {
        if (media == 9 || media == 11 ||
            ((media == 1 || media == 2 || media == 3 || media == 10) &&
             (mode_word & 0x0f00u) == 0x0500u))
            selector += 0x20u;
        else if (media == 0 && uses_alternate_plain_paper_profile(paper))
            selector += 0x10u;
    }
    if (rotated)
        selector += 0x8000u;

    uint32_t quality = selector & 0x0f0fu;
    uint32_t extension = selector & 0x0030u;
    bool multilevel = (selector & 0x2000u) != 0;
    bool valid;
    if (ordered)
        valid = quality == 0x107u || quality == 0x205u || quality == 0x300u || quality == 0x500u;
    else if (!multilevel)
        valid = (quality == 0x107u || quality == 0x205u || quality == 0x300u) &&
                (extension == 0 || extension == 0x10u);
    else if (quality == 0x107u || quality == 0x205u || quality == 0x300u)
        valid = extension == 0 || extension == 0x10u;
    else if (quality == 0x500u)
        valid = extension == 0 || extension == 0x10u || extension == 0x20u;
    else
        valid = (quality == 0x600u || quality == 0x700u) &&
                (extension == 0 || extension == 0x20u);
    if (!valid) {
        selector = 0x8107u;
        quality = 0x107u;
        extension = 0;
        multilevel = false;
    }
    config->rotated = (selector & 0x8000u) != 0;
    config->ordered = ordered;
    config->bits_per_pixel = !ordered && multilevel ? 2u : 1u;
    config->pixels_per_byte = 8u / config->bits_per_pixel;
    config->horizontal_repeat = quality == 0x107u ? 4u : quality == 0x205u ? 2u :
        (!ordered && multilevel && quality >= 0x500u ? 2u : 1u);
    config->double_scan = !ordered && multilevel && quality >= 0x500u;
    config->plane_stride = quality >= 0x500u ? 3088u : 1544u;
    config->reverse_truncate = config->double_scan;
    config->noise_limit = config->double_scan ? 2046u : 1023u;
    config->binary_threshold = 2;

    int16_t values[8] = {0, 0, 0, 1023, 1023, 1023, 0, 420};
    if (multilevel) {
        const int16_t standard[8] = {422, 302, 0, 1023, 422, 302, 0, 302};
        memcpy(values, standard, sizeof values);
        if (config->double_scan) {
            const int16_t high[8] = {800, 600, 0, 1536, 800, 600, 0, 540};
            memcpy(values, high, sizeof values);
        }
        if (extension == 0x10u) {
            values[1] = 0;
            values[2] = 9999;
            values[7] = 420;
        } else if (extension == 0x20u) {
            if (quality == 0x500u)
                values[0] = 32;
            else {
                const int16_t glossy[8] = {600, 480, 0, 1536, 600, 480, 0, 420};
                memcpy(values, glossy, sizeof values);
            }
        }
    }
    for (size_t channel = 0; channel < 4; ++channel)
        memcpy(config->quantization[channel], values, sizeof values);
    return 0;
}

size_t bh7_rows_output_size(const Bh7RowsConfig *config, size_t input_rows)
{
    if (!config || !config->plane_stride)
        return 0;
    size_t multiplier = config->double_scan ? 8u : 4u;
    if (input_rows > SIZE_MAX / multiplier / config->plane_stride)
        return 0;
    return input_rows * multiplier * config->plane_stride;
}

Bh7Rows *bh7_rows_create(const Bh7RowsConfig *config, const Bh7RowsTables *tables,
                         const Bh7ColorProfile *color,
                         const Bh7ColorFinishProfile *finish,
                         const uint16_t *direct_black_tone, size_t axis_width,
                         bool monochrome_only, uint32_t ordered_phase)
{
    if (!config || !tables || !color || !axis_width ||
        !config->horizontal_repeat || !config->plane_stride ||
        (config->bits_per_pixel != 1 && config->bits_per_pixel != 2) ||
        config->pixels_per_byte != 8u / config->bits_per_pixel ||
        (monochrome_only && !direct_black_tone) || (!monochrome_only && !finish))
        return NULL;
    if ((config->ordered && !tables->ordered_matrix) ||
        (!config->ordered && (!tables->initial_errors || !tables->noise || !tables->minimum)))
        return NULL;
    if (axis_width > (SIZE_MAX / sizeof(int16_t) / 2u - 4u) / config->horizontal_repeat)
        return NULL;
    Bh7Rows *rows = calloc(1, sizeof *rows);
    if (!rows)
        return NULL;
    rows->config = *config;
    rows->tables = *tables;
    rows->color = *color;
    if (finish)
        rows->finish = *finish;
    rows->direct_black_tone = direct_black_tone;
    rows->axis_width = axis_width;
    rows->monochrome_only = monochrome_only;
    rows->ordered_phase = ordered_phase;
    rows->error_count = (axis_width * config->horizontal_repeat + 4u) * 2u;
    if (!config->ordered) {
        for (size_t channel = 0; channel < 4; ++channel) {
            rows->errors[channel] = malloc(rows->error_count * sizeof(int16_t));
            if (!rows->errors[channel]) {
                bh7_rows_destroy(rows);
                return NULL;
            }
        }
    }
    bh7_rows_reset(rows);
    return rows;
}

void bh7_rows_destroy(Bh7Rows *rows)
{
    if (rows) {
        for (size_t channel = 0; channel < 4; ++channel)
            free(rows->errors[channel]);
        free(rows);
    }
}

int bh7_rows_set_profiles(Bh7Rows *rows, const Bh7RowsTables *tables,
                          const Bh7ColorProfile *color,
                          const Bh7ColorFinishProfile *finish,
                          const uint16_t *direct_black_tone)
{
    if (!rows || !tables || !color ||
        (rows->monochrome_only && !direct_black_tone) ||
        (!rows->monochrome_only && !finish) ||
        (rows->config.ordered && !tables->ordered_matrix) ||
        (!rows->config.ordered && (!tables->initial_errors || !tables->noise || !tables->minimum)))
        return -1;
    rows->tables = *tables;
    rows->color = *color;
    if (finish)
        rows->finish = *finish;
    else
        memset(&rows->finish, 0, sizeof rows->finish);
    rows->direct_black_tone = direct_black_tone;
    return 0;
}

void bh7_rows_reset(Bh7Rows *rows)
{
    if (!rows)
        return;
    static const unsigned pattern[4] = {3, 0, 1, 2};
    if (!rows->config.ordered) {
        for (size_t channel = 0; channel < 4; ++channel) {
            for (size_t word = 0; word < rows->error_count; ++word)
                rows->errors[channel][word] = rows->tables.initial_errors[pattern[channel]][word & 255u];
        }
    }
    rows->phase = 0;
    rows->random_index = 0;
}

static int convert_source_pixel(Bh7Rows *rows, const uint8_t *rgb, uint32_t *last,
                    uint16_t cached[4])
{
    uint32_t pixel = rgb[0] | (uint32_t)rgb[1] << 8 | (uint32_t)rgb[2] << 16;
    if (pixel == *last)
        return 0;
    uint16_t separated[4];
    if (bh7_color_convert_pixel(&rows->color, rgb, separated))
        return -1;
    if (rows->monochrome_only) {
        cached[0] = cached[1] = cached[2] = 0;
        cached[3] = rows->direct_black_tone[separated[3] & 255u];
    } else {
        int status = rows->config.ordered
            ? bh7_color_finish_shared_black(&rows->finish, separated, cached)
            : bh7_color_finish_additive(&rows->finish, separated, cached);
        if (status)
            return -1;
    }
    *last = pixel;
    return 0;
}

static int diffuse_and_pack_pixel(Bh7Rows *rows, const uint16_t input[4], size_t x,
                    uint32_t lane, bool backwards, uint8_t packed[4])
{
    static const unsigned color_index[4] = {3, 0, 1, 2};
    size_t current = x * 2u + 4u + lane;
    size_t previous = x * 2u + 4u + (lane ^ 1u);
    size_t neighbor = backwards ? current + 2u : current - 2u;
    if (current >= rows->error_count || previous >= rows->error_count || neighbor >= rows->error_count)
        return -1;
    size_t channels = rows->monochrome_only ? 1u : 4u;
    unsigned bits = rows->config.bits_per_pixel;
    for (size_t channel = 0; channel < channels; ++channel) {
        uint16_t level = input[color_index[channel]];
        if (level > 1023)
            return -1;
        int16_t *errors = rows->errors[channel];
        int32_t neighbors = wrap_to_signed_16bit((int32_t)errors[previous] + errors[neighbor]);
        int32_t average = backwards && rows->config.reverse_truncate
            ? neighbors / 2 : divide_by_two_rounding_down(neighbors);
        int16_t adjusted = wrap_to_signed_16bit(average + level);
        rows->random_index &= 1023u;
        rows->noise_index &= 1023u;
        int16_t minimum = rows->tables.minimum[level];
        if (adjusted < minimum)
            adjusted = wrap_to_signed_16bit((int32_t)minimum + rows->tables.noise[rows->noise_index] / 4);
        int16_t noise = rows->tables.noise[rows->noise_index];
        ++rows->noise_index;
        int16_t threshold = 2;
        if (noise > 191) {
            threshold = wrap_to_signed_16bit((int32_t)rows->tables.noise[rows->random_index] * 2 + 514);
            ++rows->random_index;
            if (threshold < 2)
                threshold = 2;
            else if (threshold > (int16_t)rows->config.noise_limit)
                threshold = (int16_t)rows->config.noise_limit;
        }
        const int16_t *quantization = rows->config.quantization[channel];
        unsigned code = 0;
        if (!level) {
            adjusted = wrap_to_signed_16bit((int32_t)adjusted + 1);
            if ((int32_t)quantization[6] + threshold <= adjusted)
                adjusted = wrap_to_signed_16bit((int32_t)adjusted - quantization[7]);
        } else if (bits == 1) {
            unsigned choice = rows->config.binary_threshold;
            if (choice > 2)
                return -1;
            if (adjusted >= (int32_t)quantization[choice] + threshold) {
                code = 1;
                adjusted = wrap_to_signed_16bit((int32_t)adjusted - quantization[choice + 3u]);
            }
        } else {
            for (unsigned choice = 0; choice < 3; ++choice) {
                if (adjusted >= (int32_t)quantization[choice] + threshold) {
                    code = 3u - choice;
                    adjusted = wrap_to_signed_16bit((int32_t)adjusted - quantization[choice + 3u]);
                    break;
                }
            }
        }
        packed[channel] = backwards
            ? (uint8_t)((packed[channel] >> bits) | (code << (8u - bits)))
            : (uint8_t)((packed[channel] << bits) | code);
        errors[current] = adjusted;
    }
    return 0;
}

static void write_packed_color_bytes(uint8_t *output, size_t stride, size_t row,
                         size_t byte, const uint8_t packed[4], uint32_t *nonzero)
{
    static const unsigned input_channel[4] = {1, 2, 3, 0};
    uint32_t summary = 0;
    for (size_t channel = 0; channel < 4; ++channel) {
        uint8_t value = packed[input_channel[channel]];
        output[(row * 4u + channel) * stride + byte] = value;
        summary |= value;
    }
    if (!*nonzero)
        *nonzero = summary;
}

static int render_error_diffusion_rows(Bh7Rows *rows, uint32_t first_row, uint32_t axis_width,
                            const Bh7RowsBand *band, uint32_t row_count,
                            uint32_t first_column, uint32_t column_count,
                            uint8_t *output, Bh7RowsResult *result)
{
    const Bh7RowsConfig *config = &rows->config;
    size_t repeat = config->horizontal_repeat;
    size_t first = (config->rotated ? axis_width - first_column - column_count : first_column) * repeat;
    size_t count = column_count * repeat;
    unsigned scans = config->double_scan ? 2u : 1u;
    uint32_t seed = rows->noise_index;
    uint32_t last_pixel = 0xffffffu;
    uint16_t cached[4] = {0, 0, 0, 0};
    for (size_t row = 0; row < row_count; ++row) {
        for (unsigned scan = 0; scan < scans; ++scan) {
            size_t destination_row = row * scans + scan;
            bool backwards = config->double_scan ? scan != 0 : (row & 1u) != 0;
            if (!backwards) {
                uint32_t step = (uint32_t)(int32_t)rows->tables.noise[(first_row + row) & 1023u];
                seed = seed + step;
                rows->noise_index = seed;
            }
            uint8_t packed[4] = {0, 0, 0, 0};
            size_t physical = backwards && count ? first + count - 1u : first;
            size_t byte = physical / config->pixels_per_byte;
            size_t occupied = physical % config->pixels_per_byte;
            if (backwards)
                occupied = config->pixels_per_byte - 1u - occupied;
            for (size_t dot = 0; dot < count; ++dot) {
                size_t source_column = backwards ? column_count - 1u - dot / repeat : dot / repeat;
                size_t x = config->rotated ? first_row + row : first_column + source_column;
                size_t y = config->rotated ? first_column + column_count - 1u - source_column : first_row + row;
                if (convert_source_pixel(rows, band->pixels + y * band->row_stride + x * 3u, &last_pixel, cached) ||
                    diffuse_and_pack_pixel(rows, cached, physical, rows->phase ^ (uint32_t)(destination_row & 1u), backwards, packed))
                    return -1;
                if (++occupied == config->pixels_per_byte) {
                    if (byte >= config->plane_stride)
                        return -1;
                    write_packed_color_bytes(output, config->plane_stride, destination_row, byte, packed, &result->nonzero);
                    occupied = 0;
                    byte = backwards ? byte - 1u : byte + 1u;
                }
                physical = backwards ? physical - 1u : physical + 1u;
            }
            if (occupied) {
                if (byte >= config->plane_stride)
                    return -1;
                unsigned shift = (unsigned)(config->pixels_per_byte - occupied) * config->bits_per_pixel;
                for (size_t channel = 0; channel < 4; ++channel)
                    packed[channel] = backwards ? (uint8_t)(packed[channel] >> shift) : (uint8_t)(packed[channel] << shift);
                write_packed_color_bytes(output, config->plane_stride, destination_row, byte, packed, &result->nonzero);
            }
        }
    }
    rows->noise_index = seed;
    rows->phase = (rows->phase + (config->double_scan ? 0u : row_count)) & 1u;
    return 0;
}

static int render_ordered_dither_rows(Bh7Rows *rows, uint32_t first_row, uint32_t axis_width,
                          const Bh7RowsBand *band, uint32_t row_count,
                          uint32_t first_column, uint32_t column_count,
                          uint8_t *output, Bh7RowsResult *result)
{
    const Bh7RowsConfig *config = &rows->config;
    size_t repeat = config->horizontal_repeat;
    size_t count = column_count * repeat;
    if (!count || !row_count)
        return 0;
    size_t padding = ((size_t)axis_width + 7u) / 8u * 8u - axis_width;
    size_t matrix_first = (config->rotated ? padding + first_column : first_column) * repeat;
    size_t first = config->rotated ? ((size_t)axis_width - first_column - column_count) * repeat : first_column * repeat;
    size_t first_byte = first / 8u;
    size_t last_byte = (first + count - 1u) / 8u;
    if (last_byte >= config->plane_stride)
        return -1;
    uint32_t matrix_row = (rows->ordered_phase + first_row) & 127u;
    if (row_count > 128u - matrix_row)
        return -1;
    for (size_t row = 0; row < row_count; ++row) {
        for (size_t channel = 0; channel < 4; ++channel)
            memset(output + (row * 4u + channel) * config->plane_stride + first_byte, 0, last_byte - first_byte + 1u);
    }
    uint32_t last_pixel = 0xffffffu;
    uint16_t cached[4] = {0, 0, 0, 0};
    size_t outer_count = config->rotated ? count : row_count;
    size_t inner_count = config->rotated ? row_count : count;
    for (size_t outer = 0; outer < outer_count; ++outer) {
        for (size_t inner = 0; inner < inner_count; ++inner) {
            size_t row = config->rotated ? inner : outer;
            size_t dot = config->rotated ? outer : inner;
            size_t source_column = first_column + dot / repeat;
            size_t x = config->rotated ? first_row + row : source_column;
            size_t y = config->rotated ? source_column : first_row + row;
            if (convert_source_pixel(rows, band->pixels + y * band->row_stride + x * 3u, &last_pixel, cached))
                return -1;
            unsigned threshold = rows->tables.ordered_matrix[((size_t)matrix_row + row) * 128u + ((matrix_first + dot) & 127u)] * 4u;
            size_t physical = config->rotated ? first + count - 1u - dot : first + dot;
            size_t byte = physical / 8u;
            uint8_t bit = (uint8_t)(1u << (7u - physical % 8u));
            for (size_t channel = 0; channel < 4; ++channel) {
                if (threshold < cached[channel])
                    output[(row * 4u + channel) * config->plane_stride + byte] |= bit;
            }
        }
    }
    for (size_t row = 0; row < row_count; ++row) {
        for (size_t byte = first_byte; byte <= last_byte; ++byte) {
            uint8_t black = output[(row * 4u + 3u) * config->plane_stride + byte];
            result->nonzero |= black;
            for (size_t channel = 0; channel < 3; ++channel) {
                uint8_t *value = output + (row * 4u + channel) * config->plane_stride + byte;
                result->nonzero |= *value;
                *value &= (uint8_t)~black;
            }
        }
    }
    return 0;
}

int bh7_rows_render(Bh7Rows *rows, uint32_t first_row, uint32_t axis_width,
                    const Bh7RowsBand *band, uint32_t row_count,
                    uint32_t first_column, uint32_t column_count,
                    uint8_t *output, size_t output_size, Bh7RowsResult *result)
{
    if (!rows || !band || !band->pixels || !output || !result ||
        !band->columns || !band->rows || band->columns > SIZE_MAX / 3u ||
        band->row_stride < band->columns * 3u || axis_width != rows->axis_width ||
        first_column > axis_width || column_count > axis_width - first_column)
        return -1;
    size_t source_rows = rows->config.rotated ? band->columns : band->rows;
    size_t source_columns = rows->config.rotated ? band->rows : band->columns;
    if (axis_width > source_columns || first_row > source_rows || row_count > source_rows - first_row)
        return -1;
    size_t required = bh7_rows_output_size(&rows->config, row_count);
    if ((!required && row_count) || required > output_size)
        return -1;
    result->nonzero = 0;
    result->output_rows = (size_t)row_count * (rows->config.double_scan ? 2u : 1u);
    if (rows->config.ordered)
        return render_ordered_dither_rows(rows, first_row, axis_width, band, row_count, first_column, column_count, output, result);
    return render_error_diffusion_rows(rows, first_row, axis_width, band, row_count, first_column, column_count, output, result);
}
