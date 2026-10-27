/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "pipeline.h"
#include "planes.h"

#include <ctype.h>
#include <errno.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

enum { INPUT_BAND_ROWS = 512 };

static int report_error(char *error, size_t capacity, const char *format, ...) {
    if (error && capacity) {
        va_list arguments;
        va_start(arguments, format);
        vsnprintf(error, capacity, format, arguments);
        va_end(arguments);
    }
    return -1;
}

static int read_header_token(FILE *input, char *token, size_t capacity) {
    int character;
    for (;;) {
        character = fgetc(input);
        if (character == EOF) return ferror(input) ? -1 : 0;
        if (character == '#') {
            do { character = fgetc(input); } while (character != EOF && character != '\n' && character != '\r');
            if (character == EOF) return ferror(input) ? -1 : 0;
        } else if (!isspace((unsigned char)character)) break;
    }
    size_t length = 0;
    while (!isspace((unsigned char)character)) {
        if (length + 1 >= capacity) return -1;
        token[length++] = (char)character;
        character = fgetc(input);
        if (character == EOF) return -1;
    }
    token[length] = '\0';
    if (character == '\r') {
        int following = fgetc(input);
        if (following != '\n' && following != EOF && ungetc(following, input) == EOF) return -1;
    }
    return 1;
}

static int read_page_header(FILE *input, const Bh7DeviceConfig *device,
                             char *error, size_t error_capacity) {
    char token[64];
    int token_status = read_header_token(input, token, sizeof(token));
    if (token_status == 0) return 1;
    if (token_status < 0 || strcmp(token, "P6"))
        return report_error(error, error_capacity, "The input is not an 8-bit P6 portable pixmap stream.");
    uint32_t dimensions[3];
    for (size_t field_index = 0; field_index < 3; ++field_index) {
        if (read_header_token(input, token, sizeof(token)) != 1 || !isdigit((unsigned char)token[0]))
            return report_error(error, error_capacity, "The portable pixmap header is incomplete or invalid.");
        char *end;
        errno = 0;
        unsigned long value = strtoul(token, &end, 10);
        if (errno || *end || !value || value > UINT32_MAX)
            return report_error(error, error_capacity, "The portable pixmap header contains an invalid number.");
        dimensions[field_index] = (uint32_t)value;
    }
    if (dimensions[2] != 255)
        return report_error(error, error_capacity, "The portable pixmap must use eight-bit channels with a maximum value of 255.");
    if (dimensions[0] != device->raster_width || dimensions[1] != device->raster_height)
        return report_error(error, error_capacity,
            "The input page is %lu by %lu pixels; the selected paper and quality require %lu by %lu pixels.",
            (unsigned long)dimensions[0], (unsigned long)dimensions[1],
            (unsigned long)device->raster_width, (unsigned long)device->raster_height);
    return 0;
}

static int write_printer_bytes(void *opaque, const uint8_t *bytes, size_t length) {
    FILE *output = opaque;
    return fwrite(bytes, 1, length, output) == length ? 0 : -1;
}

static uint32_t page_feed_limit(const Bh7DeviceConfig *device) {
    uint32_t printable_extent = device->rotated ? device->printable_width_600 : device->printable_height_600;
    uint32_t quarter_extent = printable_extent / 4;
    return quarter_extent < 282 ? 0 : (quarter_extent * 24000 - 6768000) / 150;
}

static uint32_t rows_per_chunk(uint16_t mode_word) {
    uint16_t resolution_class = mode_word & 0xf00;
    return resolution_class >= 0x500 && resolution_class <= 0x700 ? 4 : 16;
}

int bh7_pipeline_render_ppm(FILE *pixel_input, FILE *printer_output,
                            const Bh7DeviceConfig *device,
                            const Bh7RasterProfileProvider *profiles,
                            size_t *rendered_page_count,
                            char *error, size_t error_capacity) {
    if (error && error_capacity) error[0] = '\0';
    if (rendered_page_count) *rendered_page_count = 0;
    if (!pixel_input || !printer_output || !device || !profiles || !profiles->select || !device->raster_width ||
        !device->raster_height || device->rotated)
        return report_error(error, error_capacity, "The printer pipeline configuration is invalid.");

    int header_status = read_page_header(pixel_input, device, error, error_capacity);
    if (header_status != 0) return header_status < 0 ? -1 :
        report_error(error, error_capacity, "The input stream contains no printable pages.");

    Bh7RowsConfig row_configuration;
    if (bh7_rows_configure(&row_configuration, device->effective_mode_word,
                          device->internal_media, device->paper_code,
                          device->render_class == 1, device->rotated) < 0)
        return report_error(error, error_capacity, "The selected paper and quality have no valid raster configuration.");
    Bh7RasterProfile active_profile;
    if (profiles->select(profiles->opaque, device->render_class, device->effective_mode_word,
                         &active_profile, error, error_capacity) < 0) {
        if (error && error_capacity && !error[0]) report_error(error, error_capacity, "Printer calibration could not be selected.");
        return -1;
    }
    bool monochrome_only = device->color_mode == 1 && (device->effective_mode_word & 0x5000) != 0x5000;
    Bh7Rows *row_renderer = bh7_rows_create(&row_configuration, &active_profile.rows,
        &active_profile.color, &active_profile.finish, active_profile.direct_black_tone,
        device->raster_width, monochrome_only, 0);
    if (!row_renderer)
        return report_error(error, error_capacity, "The raster renderer could not allocate its working memory or load its calibration tables.");

    Bh7PlanesConfig plane_configuration = {
        .mode_word = device->mode_word, .codec = device->codec, .head_type = device->head_type,
        .mirror = device->settings.mirror, .rotated = device->rotated,
        .feed_dpi = device->feed_dpi, .initial_mode = device->initial_mode,
        .output_flags = device->output_flags, .feed_limit = device->feed_limit,
    };
    Bh7Planes *plane_writer = bh7_planes_create(&plane_configuration, write_printer_bytes, printer_output);
    if (!plane_writer) {
        bh7_rows_destroy(row_renderer);
        return report_error(error, error_capacity, "The printer packet writer could not allocate its working memory.");
    }

    size_t pixel_row_bytes = (size_t)device->raster_width * 3;
    size_t maximum_band_rows = device->raster_height < INPUT_BAND_ROWS ? device->raster_height : INPUT_BAND_ROWS;
    size_t maximum_chunk_rows = rows_per_chunk(device->effective_mode_word);
    size_t packed_capacity = bh7_rows_output_size(&row_configuration, maximum_chunk_rows);
    uint8_t *band_pixels = malloc(pixel_row_bytes * maximum_band_rows);
    uint8_t *packed_planes = malloc(packed_capacity ? packed_capacity : 1);
    int result = -1;
    size_t page_count = 0;
    if (!band_pixels || !packed_planes || !packed_capacity) {
        report_error(error, error_capacity, "There is not enough memory to render a printer raster band.");
        goto cleanup;
    }
    if (bh7_device_job_start(device, write_printer_bytes, printer_output) < 0) {
        report_error(error, error_capacity, "The printer output could not be initialized.");
        goto cleanup;
    }

    Bh7DeviceConfig page_configuration = *device;
    size_t band_count = ((size_t)device->raster_height + INPUT_BAND_ROWS - 1) / INPUT_BAND_ROWS;
    size_t final_band_rows = device->raster_height % INPUT_BAND_ROWS;
    if (!final_band_rows) final_band_rows = INPUT_BAND_ROWS;
    do {
        if (bh7_planes_begin_page(plane_writer, page_configuration.initial_mode, page_feed_limit(device)) < 0 ||
            bh7_device_page_start(&page_configuration, write_printer_bytes, printer_output) < 0) {
            report_error(error, error_capacity, "The printer output could not start the next page.");
            goto cleanup;
        }
        uint32_t page_ink_mask = 0;
        for (size_t band_index = 0; band_index < band_count; ++band_index) {
            size_t band_rows = band_index + 1 == band_count ? final_band_rows : INPUT_BAND_ROWS;
            size_t band_bytes = pixel_row_bytes * band_rows;
            if (fread(band_pixels, 1, band_bytes, pixel_input) != band_bytes) {
                report_error(error, error_capacity, "The portable pixmap page ended before all of its pixel data could be read.");
                goto cleanup;
            }
            Bh7RowsBand band = {
                .pixels = band_pixels, .row_stride = pixel_row_bytes,
                .columns = device->raster_width, .rows = band_rows,
            };
            Bh7PlanesBandInfo band_information;
            if (bh7_planes_begin_band(plane_writer, &band, band_index, band_count, final_band_rows,
                                      device->effective_mode_word, device->render_class, &band_information) < 0) {
                report_error(error, error_capacity, "The printer packet writer could not initialize a raster band.");
                goto cleanup;
            }
            page_configuration.mode_word = device->effective_mode_word;
            page_configuration.initial_mode = bh7_mode_byte(device->effective_mode_word);
            if (profiles->select(profiles->opaque, device->render_class, device->effective_mode_word,
                                 &active_profile, error, error_capacity) < 0 ||
                bh7_rows_set_profiles(row_renderer, &active_profile.rows, &active_profile.color, &active_profile.finish,
                                       active_profile.direct_black_tone) < 0) {
                if (error && error_capacity && !error[0]) report_error(error, error_capacity, "Printer calibration could not be updated for the next raster band.");
                goto cleanup;
            }
            if (!page_ink_mask && device->render_class == 2) bh7_rows_reset(row_renderer);

            for (size_t first_row = 0; first_row + 1 < band_rows;) {
                size_t chunk_rows = band_rows - first_row;
                if (chunk_rows > maximum_chunk_rows) chunk_rows = maximum_chunk_rows;
                Bh7RowsResult rendered = {0};
                /* Pixels outside the selected ink bounds represent white paper.
                 * Row conversion writes only the selected columns, while the
                 * packet encoder consumes complete plane rows. */
                if (band_information.has_content)
                    memset(packed_planes, 0, bh7_rows_output_size(&row_configuration, chunk_rows));
                if (band_information.has_content &&
                    bh7_rows_render(row_renderer, (uint32_t)first_row, device->raster_width,
                        &band, (uint32_t)chunk_rows, (uint32_t)band_information.first_column,
                        (uint32_t)band_information.column_count, packed_planes, packed_capacity, &rendered) < 0) {
                    report_error(error, error_capacity, "A raster band could not be converted into printer ink planes.");
                    goto cleanup;
                }
                size_t packed_length = band_information.has_content ?
                    bh7_rows_output_size(&row_configuration, chunk_rows) : 0;
                if (bh7_planes_push(plane_writer, band_information.has_content ? packed_planes : NULL,
                                    packed_length, chunk_rows, rendered.nonzero != 0) < 0) {
                    report_error(error, error_capacity, "A raster band could not be encoded into printer packets.");
                    goto cleanup;
                }
                page_ink_mask |= rendered.nonzero;
                first_row += chunk_rows;
            }
            if (bh7_planes_end_band(plane_writer, band_index + 1 == band_count) < 0) {
                report_error(error, error_capacity, "The printer packet writer could not finish a raster band.");
                goto cleanup;
            }
        }
        if (bh7_planes_end_page(plane_writer) < 0) {
            report_error(error, error_capacity, "The printer output could not eject the completed page.");
            goto cleanup;
        }
        ++page_count;
        if (rendered_page_count) *rendered_page_count = page_count;
        header_status = read_page_header(pixel_input, device, error, error_capacity);
        if (header_status < 0) goto cleanup;
    } while (header_status == 0);
    if (bh7_planes_end_job(plane_writer) < 0 || bh7_device_job_end(write_printer_bytes, printer_output) < 0 ||
        fflush(printer_output) != 0) {
        report_error(error, error_capacity, "The printer output could not finish the print job.");
        goto cleanup;
    }
    result = 0;
cleanup:
    free(packed_planes);
    free(band_pixels);
    bh7_planes_destroy(plane_writer);
    bh7_rows_destroy(row_renderer);
    return result;
}
