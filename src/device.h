/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef BH7_DEVICE_H
#define BH7_DEVICE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    BH7_DRAFT, BH7_FAST, BH7_NORMAL, BH7_FINE, BH7_PHOTO, BH7_HIGHEST
} Bh7Quality;

typedef enum {
    BH7_PLAIN, BH7_INKJET, BH7_BROTHER_GLOSSY, BH7_GLOSSY, BH7_TRANSPARENCIES
} Bh7Media;

typedef enum { BH7_GRAPHICS = 1, BH7_DOCUMENT_PHOTO = 2, BH7_CUSTOM = 3 } Bh7Document;
typedef enum { BH7_DITHER = 1, BH7_DIFFUSION = 2 } Bh7Halftone;
typedef enum { BH7_VIVID = 0, BH7_NATURAL = 1, BH7_NO_MATCH = 2 } Bh7ColorMatch;

typedef struct {
    Bh7Quality quality;
    Bh7Media media;
    Bh7Document document;
    Bh7Halftone halftone;
    Bh7ColorMatch color_match;
    const char *paper_name;
    uint8_t paper_source; /* 0 automatic, 1 tray one, 2 tray two. */
    bool monochrome;
    bool thick_paper;
    bool bidirectional;
    bool mirror;
    bool color_enhance;
    bool slow_drying;
    int8_t brightness;
    int8_t contrast;
    int8_t red;
    int8_t green;
    int8_t blue;
} Bh7PrintSettings;

typedef struct {
    Bh7PrintSettings settings;
    uint16_t mode_word; /* Setup mode, before the document's row selection. */
    uint16_t effective_mode_word;
    int16_t internal_media;
    int16_t paper_code;
    uint16_t feed_dpi;
    uint8_t head_type;
    uint8_t codec;
    uint8_t initial_mode;
    uint8_t output_flags;
    uint8_t render_class; /* 1 ordered/neutral, 2 error diffusion/color. */
    uint8_t thickness_code;
    uint8_t source_code;
    uint8_t color_mode; /* 1 monochrome, 2 color. */
    uint8_t raster_parameter;
    bool borderless;
    bool rotated;
    uint32_t feed_limit;
    uint32_t raster_width;
    uint32_t raster_height;
    uint32_t paper_width_600;
    uint32_t paper_height_600;
    uint32_t page_width_600;
    uint32_t page_height_600;
    uint32_t printable_width_600;
    uint32_t printable_height_600;
    int32_t origin_primary_600;
    int32_t origin_secondary_600;
    uint16_t page_width_120;
    uint16_t page_height_120;
    uint16_t custom_width_tenths_mm;
    int16_t color_adjustment[5]; /* brightness, contrast, red, green, blue. */
} Bh7DeviceConfig;

/* A writer returns zero on success and a negative value on failure. */
typedef int (*Bh7OutputWriter)(void *opaque, const uint8_t *data, size_t length);

void bh7_print_defaults(Bh7PrintSettings *settings);
int16_t bh7_paper_code(const char *name);
uint8_t bh7_mode_byte(uint16_t mode_word);
int bh7_device_configure(Bh7DeviceConfig *device, const Bh7PrintSettings *settings,
                         uint32_t paper_width_600, uint32_t paper_height_600);
int bh7_device_job_start(const Bh7DeviceConfig *device, Bh7OutputWriter write, void *opaque);
int bh7_device_page_start(const Bh7DeviceConfig *device, Bh7OutputWriter write, void *opaque);
int bh7_device_job_end(Bh7OutputWriter write, void *opaque);

#endif
