/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "device.h"

#include <string.h>

struct paper_identity {
    const char *name;
    int16_t code;
};

static const struct paper_identity papers[] = {
    {"A4", 9}, {"BrA4_B", 9}, {"Letter", 1}, {"BrLetter_B", 1},
    {"Legal", 5}, {"Executive", 7}, {"B5", 34}, {"A5", 11},
    {"A6", 70}, {"BrA6_B", 70}, {"PostC4x6", 265}, {"BrPostC4x6_B", 265},
    {"IndexC5x8", 266}, {"BrIndexC5x8_B", 266}, {"PhotoL", 272}, {"BrPhotoL_B", 272},
    {"Photo2L", 273}, {"BrPhoto2L_B", 273}, {"PostCard", 43}, {"BrHagaki_B", 43},
    {"DoublePostCardRotated", 82}, {"EnvC5", 28}, {"EnvDL", 27},
    {"Env10", 20}, {"EnvMonarch", 37}, {"EnvYou4", 91},
};

static bool envelope_code(int16_t code) {
    return code == 34 || code == 28 || code == 91 || code == 73 ||
           code == 30 || code == 20 || code == 27 || code == 37;
}

static bool density_quality(uint16_t mode_word) {
    switch (mode_word & 0xf00) {
    case 0x200: case 0x300: case 0x500: case 0x600: case 0x700: return true;
    default: return false;
    }
}

static int32_t points_to_raster(int32_t points) {
    return (int32_t)((float)points * 8.333333f + 0.5f);
}

void bh7_print_defaults(Bh7PrintSettings *settings) {
    if (!settings) return;
    memset(settings, 0, sizeof(*settings));
    settings->quality = BH7_NORMAL;
    settings->media = BH7_PLAIN;
    settings->document = BH7_DOCUMENT_PHOTO;
    settings->halftone = BH7_DIFFUSION;
    settings->color_match = BH7_NATURAL;
    settings->paper_name = "A4";
    settings->bidirectional = true;
}

int16_t bh7_paper_code(const char *name) {
    if (!name) return -1;
    for (size_t entry_index = 0; entry_index < sizeof(papers) / sizeof(papers[0]); ++entry_index)
        if (!strcmp(name, papers[entry_index].name)) return papers[entry_index].code;
    return -1;
}

uint8_t bh7_mode_byte(uint16_t mode_word) {
    bool use_separate_black_plane = (mode_word & 0x5000) == 0x1000;
    if (mode_word & 0x2000) return use_separate_black_plane ? 0x30 : 0x31;
    return use_separate_black_plane ? 0 : 0x10;
}

int bh7_device_configure(Bh7DeviceConfig *device, const Bh7PrintSettings *settings,
                         uint32_t paper_width_600, uint32_t paper_height_600) {
    static const uint16_t base_mode[] = {0x107, 0x205, 0x300, 0x500, 0x600, 0x700};
    static const int16_t internal_media[] = {0, 1, 9, 3, 4};
    if (!device || !settings || !settings->paper_name ||
        (unsigned)settings->quality > BH7_HIGHEST || (unsigned)settings->media > BH7_TRANSPARENCIES ||
        settings->document < BH7_GRAPHICS || settings->document > BH7_CUSTOM ||
        settings->halftone < BH7_DITHER || settings->halftone > BH7_DIFFUSION ||
        (unsigned)settings->color_match > BH7_NO_MATCH || settings->paper_source > 2 ||
        !paper_width_600 || !paper_height_600 || paper_width_600 > 16000 || paper_height_600 > 30000)
        return -1;
    const int8_t requested_adjustments[] = {settings->brightness, settings->contrast, settings->red, settings->green, settings->blue};
    for (size_t entry_index = 0; entry_index < sizeof(requested_adjustments); ++entry_index) if (requested_adjustments[entry_index] < -20 || requested_adjustments[entry_index] > 20) return -1;
    int16_t paper_identifier = bh7_paper_code(settings->paper_name);
    if (paper_identifier < 0) return -1;
    memset(device, 0, sizeof(*device));
    device->settings = *settings;
    device->internal_media = internal_media[settings->media];
    device->paper_code = paper_identifier;
    device->paper_width_600 = paper_width_600;
    device->paper_height_600 = paper_height_600;
    device->feed_dpi = settings->quality == BH7_DRAFT ? 150 : settings->quality == BH7_FAST ? 300 : 600;
    device->raster_width = paper_width_600 * device->feed_dpi / 600;
    device->raster_height = paper_height_600 * device->feed_dpi / 600;
    device->borderless = strstr(settings->paper_name, "_B") != NULL;
    device->color_mode = settings->monochrome ? 1 : 2;
    device->codec = 3;
    device->source_code = settings->paper_source ? settings->paper_source : 7;

    uint32_t page_width_points = paper_width_600 * 72 / 600 + 32;
    uint32_t page_height_points = paper_height_600 * 72 / 600 + 28;
    uint32_t primary_margin_points = device->borderless && settings->quality == BH7_DRAFT ? 0 : 14;
    uint32_t secondary_margin_points = device->borderless && settings->quality == BH7_DRAFT ? 0 : 16;
    device->page_width_120 = (uint16_t)(page_width_points * 120 / 72);
    device->page_height_120 = (uint16_t)(page_height_points * 120 / 72);
    device->page_width_600 = (uint32_t)device->page_width_120 * 5;
    device->page_height_600 = (uint32_t)device->page_height_120 * 5;
    device->custom_width_tenths_mm = (uint16_t)((uint32_t)device->page_width_120 * 254 / 120);
    device->origin_primary_600 = points_to_raster(-(int32_t)primary_margin_points);
    device->origin_secondary_600 = points_to_raster(-(int32_t)secondary_margin_points);
    device->printable_width_600 = (uint32_t)points_to_raster((int32_t)(page_width_points - secondary_margin_points));
    device->printable_height_600 = (uint32_t)points_to_raster((int32_t)(page_height_points - primary_margin_points));

    uint16_t mode_word = base_mode[settings->quality];
    int16_t media_identifier = device->internal_media;
    if (settings->monochrome) {
        mode_word |= 0x1000;
        if (media_identifier == 1 || media_identifier == 3 || media_identifier == 4 || media_identifier == 9 || (media_identifier == 0 && settings->slow_drying)) mode_word |= 0x5000;
    }
    if ((settings->document == BH7_DOCUMENT_PHOTO ||
         (settings->document == BH7_CUSTOM && settings->halftone == BH7_DIFFUSION)) && density_quality(mode_word)) mode_word |= 0x2000;
    if (media_identifier == 4 && ((mode_word & 0xfff) == 0x300 || (mode_word & 0xfff) == 0x500)) mode_word |= 0x10;
    device->mode_word = mode_word;
    if (media_identifier == 4) device->head_type = 4;
    else if (media_identifier == 0 && envelope_code(paper_identifier)) device->head_type = 0x10;
    else if (device->borderless && (mode_word & 0xf00) != 0x100) device->head_type = 0x80;

    device->render_class = settings->document == BH7_GRAPHICS ? 1 :
        settings->document == BH7_DOCUMENT_PHOTO ? 2 : settings->halftone == BH7_DITHER ? 1 : 2;
    if ((mode_word & 0xfff) > 0x4ff || (mode_word & 0x5000) == 0x5000 || (media_identifier == 0 && settings->slow_drying))
        device->render_class = 2;
    device->effective_mode_word = mode_word & (uint16_t)~0x2000;
    if (device->render_class == 2 && density_quality(mode_word)) device->effective_mode_word |= 0x2000;
    device->initial_mode = bh7_mode_byte(mode_word);
    if ((mode_word & 0xf00) == 0x100 && (paper_identifier == 9 || paper_identifier == 1)) device->output_flags = 5;

    uint8_t layout_identifier = device->head_type & 0x7f;
    device->raster_parameter = (mode_word & 0x30) == 0 && (layout_identifier == 0 || layout_identifier == 1) && (mode_word & 0xf00) >= 0x200 ? 1 : 0;
    if (settings->color_enhance) {
        for (size_t entry_index = 0; entry_index < sizeof(requested_adjustments); ++entry_index) device->color_adjustment[entry_index] = requested_adjustments[entry_index];
        if (settings->monochrome) device->color_adjustment[0] = -device->color_adjustment[0];
    }
    return 0;
}

static uint8_t resolution_parameter(uint16_t mode_word) {
    switch (mode_word & 0xfff) {
    case 0x100: return 0;
    case 0x102: case 0x106: case 0x132: case 0x136: return 0x11;
    case 0x103: case 0x107: case 0x133: case 0x137: return 2;
    case 0x201: case 0x205: case 0x211: case 0x215: return 2;
    case 0x505: case 0x515: return 4;
    case 0x500: case 0x510: case 0x520: case 0x525: return 3;
    case 0x600: case 0x610: case 0x620: return 3;
    case 0x700: case 0x710: case 0x720: return 3;
    default: return 2;
    }
}

static uint8_t resolution_detail(uint16_t mode_word) {
    switch (mode_word & 0xfff) {
    case 0x100: case 0x102: case 0x106: case 0x132: case 0x136:
    case 0x103: case 0x107: case 0x133: case 0x137: return 0;
    case 0x201: case 0x205: case 0x211: case 0x215: return 1;
    case 0x505: case 0x515: case 0x500: case 0x510: case 0x520: case 0x525: return 3;
    case 0x600: case 0x610: case 0x620: return 4;
    case 0x700: case 0x710: case 0x720: return 6;
    default: return 2;
    }
}

static uint8_t paper_parameter(int16_t paper_identifier) {
    static const struct { int16_t code; uint8_t parameter; } codes[] = {
        {1,0},{0x113,0},{9,1},{0x112,1},{5,2},{7,3},{13,4},{34,5},{28,6},{11,7},{88,8},
        {27,10},{70,11},{0x114,11},{20,12},{37,13},{0x107,14},{0x10b,14},{0x106,15},
        {0x108,16},{43,17},{0x115,17},{30,18},{0x10a,19},{0x119,19},{0x109,20},{0x118,20},
        {91,21},{73,22},{0x110,24},{0x116,24},{0x111,25},{0x117,25},{82,26},
    };
    for (size_t entry_index = 0; entry_index < sizeof(codes) / sizeof(codes[0]); ++entry_index) if (codes[entry_index].code == paper_identifier) return codes[entry_index].parameter;
    return 0xff;
}

static void store16(uint8_t *output, uint16_t value) {
    output[0] = (uint8_t)value;
    output[1] = (uint8_t)(value >> 8);
}

int bh7_device_job_start(const Bh7DeviceConfig *device, Bh7OutputWriter write, void *opaque) {
    static const uint8_t preamble_zeros[256] = {0};
    static const uint8_t reset_command[] = {0x1b,0x7c,1,0,9,0x1b,0x40};
    if (!device || !write) return -1;
    for (unsigned entry_index = 0; entry_index < 13; ++entry_index) if (write(opaque, preamble_zeros, sizeof(preamble_zeros)) < 0) return -1;
    return write(opaque, reset_command, sizeof(reset_command));
}

int bh7_device_page_start(const Bh7DeviceConfig *device, Bh7OutputWriter write, void *opaque) {
    if (!device || !write) return -1;
    uint8_t header[45];
    size_t header_length = 0;
    const uint8_t page_setup_commands[] = {0x1b,0x7c,1,0,9,'R',resolution_parameter(device->mode_word),resolution_detail(device->mode_word),
        'I',device->initial_mode,'M',device->codec == 2 ? 8 : device->codec == 3 ? 6 : 7,
        'D',device->settings.bidirectional ? 0 : 0xff,'N',4,device->thickness_code,'N',5,
        device->internal_media == 1 ? 1 : device->internal_media == 3 ? 2 : device->internal_media == 4 ? 4 : device->internal_media == 9 ? 5 : 0,
        'P',0x80,device->raster_parameter,']','T',device->source_code == 1 ? 1 : device->source_code == 2 ? 2 : 3,
        's',paper_parameter(device->paper_code)};
    memcpy(header, page_setup_commands, sizeof(page_setup_commands));
    header_length = sizeof(page_setup_commands);
    if (header[header_length - 1] == 0xff) {
        store16(header + header_length, (uint16_t)((uint32_t)device->custom_width_tenths_mm * 90 / 254));
        header_length += 2;
    }
    int32_t page_margins_600[] = {-device->origin_primary_600,
        (int32_t)(device->page_height_600 - device->printable_height_600),
        -device->origin_secondary_600,
        (int32_t)(device->page_width_600 - device->printable_width_600)};
    if (page_margins_600[0] < 20) for (size_t entry_index = 0; entry_index < 4; ++entry_index) page_margins_600[entry_index] = 11;
    if (device->settings.mirror) {
        uint32_t mirrored_width_pixels = device->printable_width_600 * device->feed_dpi / 600;
        uint32_t alignment_padding = (16 - mirrored_width_pixels % 16) % 16;
        page_margins_600[2] -= (int32_t)(alignment_padding * 600 / device->feed_dpi);
    }
    if (device->head_type & 0x80) for (size_t entry_index = 0; entry_index < 4; ++entry_index) page_margins_600[entry_index] -= 106;
    header[header_length++] = 'S';
    for (size_t entry_index = 0; entry_index < 4; ++entry_index) { store16(header + header_length, (uint16_t)page_margins_600[entry_index]); header_length += 2; }
    const uint8_t origin_commands[] = {'Y',0,0,'X',0,0};
    memcpy(header + header_length, origin_commands, sizeof(origin_commands));
    header_length += sizeof(origin_commands);
    return write(opaque, header, header_length);
}

int bh7_device_job_end(Bh7OutputWriter write, void *opaque) {
    static const uint8_t reset_command[] = {0x1b,0x40};
    return write ? write(opaque, reset_command, sizeof(reset_command)) : -1;
}
