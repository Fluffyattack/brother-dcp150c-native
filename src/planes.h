/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef BH7_PLANES_H
#define BH7_PLANES_H

#include "rows.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef int (*Bh7PlanesWrite)(void *opaque, const uint8_t *bytes, size_t length);

typedef struct {
    uint16_t mode_word; /* Raster quality and ink-plane format flags. */
    uint8_t codec; /* 0 literal bytes, 1 PackBits, 2 sparse zeros, 3 pixel groups. */
    uint8_t head_type; /* Nozzle arrangement selected for this paper and media. */
    bool mirror;
    bool rotated;
    uint16_t feed_dpi; /* Logical input rows per inch of paper travel. */
    uint8_t initial_mode; /* Raster-mode command established by the page header. */
    uint8_t output_flags; /* Job-level direct/deferred output state. */
    uint32_t feed_limit; /* Deferred-output threshold, in 1/24000-inch units. */
} Bh7PlanesConfig;

typedef struct {
    size_t first_column;
    size_t column_count;
    bool has_content;
    size_t plane_bytes; /* Source bytes in each packed ink-plane row. */
    size_t plane_stride; /* Distance in bytes between adjacent ink planes. */
} Bh7PlanesBandInfo;

typedef struct Bh7Planes Bh7Planes;

/* The output callback returns zero on success. */
Bh7Planes *bh7_planes_create(const Bh7PlanesConfig *config,
                             Bh7PlanesWrite write, void *opaque);
void bh7_planes_destroy(Bh7Planes *planes);

/* Call before the page's outer header. Keeps deferred output across pages. */
int bh7_planes_begin_page(Bh7Planes *planes, uint8_t initial_mode, uint32_t feed_limit);

/* Bounds follow the band's original RGB traversal and edge-pixel policy. */
int bh7_planes_bounds(const Bh7RowsBand *band, bool rotated,
                      Bh7PlanesBandInfo *information);

/* color_state: 1 monochrome, 2 color. Effective mode may change per band.
 * final_rows is the final band's logical row count after rotation handling.
 * This emits the per-band I command. The caller renders only selected bounds.
 */
int bh7_planes_begin_band(Bh7Planes *planes, const Bh7RowsBand *band,
                          size_t band_index, size_t band_count, size_t final_rows,
                          uint16_t effective_mode, unsigned color_state,
                          Bh7PlanesBandInfo *information);

/* Input is packed [row][C][M][Y][K], using information.plane_stride bytes per
 * channel. input_rows is the renderer's INPUT count before vertical repeats.
 */
int bh7_planes_push(Bh7Planes *planes, const uint8_t *packed, size_t packed_size,
                    size_t input_rows, bool has_content);
int bh7_planes_end_band(Bh7Planes *planes, bool last);

/* Page eject follows the current buffering state. */
int bh7_planes_end_page(Bh7Planes *planes);

/* Drain deferred bytes once after the final page, before the job ESC @. */
int bh7_planes_end_job(Bh7Planes *planes);

#endif
