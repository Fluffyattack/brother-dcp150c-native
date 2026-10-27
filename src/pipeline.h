/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef BH7_PIPELINE_H
#define BH7_PIPELINE_H

#include "device.h"
#include "rows.h"
#include <stdio.h>

/* Non-owning calibration views. Their storage must outlive the print job. */
typedef struct {
    Bh7ColorProfile color;
    Bh7ColorFinishProfile finish;
    Bh7RowsTables rows;
    const uint16_t *direct_black_tone;
} Bh7RasterProfile;

typedef int (*Bh7RasterProfileSelector)(void *opaque, unsigned rendering_class,
                                        uint16_t effective_mode_word,
                                        Bh7RasterProfile *selected_profile,
                                        char *error, size_t error_capacity);

typedef struct {
    Bh7RasterProfileSelector select;
    void *opaque;
} Bh7RasterProfileProvider;

/* Render one or more concatenated, 8-bit P6 pages to the printer data stream.
 * Every page must match the paper size and quality's input resolution.
 * Returns zero on success. A failure writes a complete diagnostic to error.
 */
int bh7_pipeline_render_ppm(FILE *pixel_input, FILE *printer_output,
                            const Bh7DeviceConfig *device,
                            const Bh7RasterProfileProvider *profiles,
                            size_t *rendered_page_count,
                            char *error, size_t error_capacity);

#endif
