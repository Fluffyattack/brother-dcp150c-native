/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef BH7_ROWS_H
#define BH7_ROWS_H

#include "color.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t plane_stride;
    uint32_t pixels_per_byte;
    uint32_t bits_per_pixel;
    uint32_t horizontal_repeat;
    int16_t quantization[4][8]; /* Channel order: black, cyan, magenta, yellow. */
    bool rotated;
    bool double_scan;
    bool ordered;
    bool reverse_truncate;
    uint16_t noise_limit;
    uint8_t binary_threshold;
} Bh7RowsConfig;

typedef struct {
    const int16_t (*initial_errors)[256]; /* Channel order: cyan, magenta, yellow, black. */
    const int16_t *noise; /* 1024 signed words. */
    const int16_t *minimum; /* 1024 signed words. */
    const uint8_t *ordered_matrix; /* 128 by 128. */
} Bh7RowsTables;

typedef struct {
    const uint8_t *pixels;
    size_t row_stride;
    size_t columns;
    size_t rows;
} Bh7RowsBand;

typedef struct {
    uint32_t nonzero;
    size_t output_rows;
} Bh7RowsResult;

typedef struct Bh7Rows Bh7Rows;

int bh7_rows_configure(Bh7RowsConfig *config, uint16_t mode_word,
                       int16_t media, int16_t paper, bool ordered, bool rotated);
Bh7Rows *bh7_rows_create(const Bh7RowsConfig *config, const Bh7RowsTables *tables,
                         const Bh7ColorProfile *color,
                         const Bh7ColorFinishProfile *finish,
                         const uint16_t *direct_black_tone, size_t axis_width,
                         bool monochrome_only, uint32_t ordered_phase);
void bh7_rows_destroy(Bh7Rows *rows);
void bh7_rows_reset(Bh7Rows *rows);
/* Rebind immutable calibration views without changing diffusion history. */
int bh7_rows_set_profiles(Bh7Rows *rows, const Bh7RowsTables *tables,
                          const Bh7ColorProfile *color,
                          const Bh7ColorFinishProfile *finish,
                          const uint16_t *direct_black_tone);
size_t bh7_rows_output_size(const Bh7RowsConfig *config, size_t input_rows);

/* Output is [row][C plane][M plane][Y plane][K plane], each plane_stride bytes. */
int bh7_rows_render(Bh7Rows *rows, uint32_t first_row, uint32_t axis_width,
                    const Bh7RowsBand *band, uint32_t row_count,
                    uint32_t first_column, uint32_t column_count,
                    uint8_t *output, size_t output_size, Bh7RowsResult *result);

#endif
