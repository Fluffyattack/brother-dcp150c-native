/* SPDX-License-Identifier: GPL-2.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include "profile.h"
#include "calibration.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAXIMUM_CALIBRATION_BYTES (8u * 1024u * 1024u)
#define MAXIMUM_CALIBRATION_ENTRIES 128u
#define MAXIMUM_CACHED_PROFILES 8u
#define PREPARED_CURVE_BYTES 21504u

typedef struct {
    char name[64];
    const uint8_t *bytes;
    size_t length;
} CalibrationEntry;

typedef struct {
    unsigned color_state;
    uint16_t effective_mode;
    uint16_t cube[BH7_COLOR_CUBE_WORDS];
    uint16_t tone_curves[4][BH7_COLOR_CURVE_LENGTH];
    int16_t black_correction[4][BH7_COLOR_CURVE_LENGTH];
    uint16_t direct_black_tone[256];
    uint16_t ink_limit;
} PreparedProfile;

typedef struct {
    Bh7DeviceConfig configuration;
    char *data_directory;
    uint8_t *calibration_bytes;
    CalibrationEntry entries[MAXIMUM_CALIBRATION_ENTRIES];
    size_t entry_count;
    uint8_t rgb_curves[3][256];
    uint8_t monochrome_curve[256];
    int16_t initial_errors[4][256];
    int16_t noise[1024];
    int16_t minimum[1024];
    uint8_t ordered_matrix[128 * 128];
    const uint8_t *weight_rounding;
    PreparedProfile *cached_profiles[MAXIMUM_CACHED_PROFILES];
    size_t next_cache_slot;
} ProfileStorage;

static int report_error(char *error, size_t capacity, const char *format, ...)
{
    if (error && capacity) {
        va_list arguments;
        va_start(arguments, format);
        (void)vsnprintf(error, capacity, format, arguments);
        va_end(arguments);
    }
    return -1;
}

static uint16_t read_little_endian_word(const uint8_t *bytes)
{
    return (uint16_t)((uint16_t)bytes[0] | (uint16_t)((uint16_t)bytes[1] << 8));
}

static uint32_t read_little_endian_integer(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | (uint32_t)bytes[1] << 8 |
           (uint32_t)bytes[2] << 16 | (uint32_t)bytes[3] << 24;
}

static int16_t read_signed_word(const uint8_t *bytes)
{
    uint16_t value = read_little_endian_word(bytes);
    return (int16_t)(value < 32768u ? (int32_t)value : (int32_t)value - 65536);
}

static char *data_path(const ProfileStorage *storage, const char *filename)
{
    size_t directory_length = strlen(storage->data_directory);
    size_t filename_length = strlen(filename);
    if (directory_length > SIZE_MAX - filename_length - 2u)
        return NULL;
    size_t length = directory_length + filename_length + 2u;
    char *path = malloc(length);
    if (path)
        (void)snprintf(path, length, "%s/%s", storage->data_directory, filename);
    return path;
}

static const CalibrationEntry *find_calibration_entry(const ProfileStorage *storage,
                                                      const char *name, size_t length)
{
    for (size_t entry = 0; entry < storage->entry_count; ++entry) {
        if (!strcmp(storage->entries[entry].name, name))
            return storage->entries[entry].length == length ? &storage->entries[entry] : NULL;
    }
    return NULL;
}

static int load_calibration_pack(ProfileStorage *storage, char *error, size_t error_size)
{
    char *path = data_path(storage, "calibration.dat");
    if (!path)
        return report_error(error, error_size, "There is not enough memory to locate the printer calibration data.");
    FILE *file = fopen(path, "rb");
    free(path);
    if (!file)
        return report_error(error, error_size, "The printer calibration data is missing. Reinstall the data directory.");
    if (fseek(file, 0, SEEK_END)) {
        (void)fclose(file);
        return report_error(error, error_size, "The printer calibration data cannot be read.");
    }
    long file_length = ftell(file);
    if (file_length < 16 || (unsigned long)file_length > MAXIMUM_CALIBRATION_BYTES ||
        fseek(file, 0, SEEK_SET)) {
        (void)fclose(file);
        return report_error(error, error_size, "The printer calibration file has an invalid size.");
    }
    size_t length = (size_t)file_length;
    storage->calibration_bytes = malloc(length);
    if (!storage->calibration_bytes) {
        (void)fclose(file);
        return report_error(error, error_size, "There is not enough memory to load the printer calibration data.");
    }
    size_t bytes_read = fread(storage->calibration_bytes, 1, length, file);
    int read_failed = ferror(file);
    (void)fclose(file);
    const uint8_t *bytes = storage->calibration_bytes;
    if (bytes_read != length || read_failed || memcmp(bytes, "BH7CAL1\0", 8) ||
        read_little_endian_integer(bytes + 8) != 1u)
        return report_error(error, error_size, "The printer calibration data is incomplete or has an unsupported format.");
    uint32_t count = read_little_endian_integer(bytes + 12);
    if (!count || count > MAXIMUM_CALIBRATION_ENTRIES)
        return report_error(error, error_size, "The printer calibration directory has an invalid entry count.");
    size_t position = 16;
    for (uint32_t entry_index = 0; entry_index < count; ++entry_index) {
        if (position > length || length - position < 68u || !memchr(bytes + position, 0, 64))
            return report_error(error, error_size, "The printer calibration directory is truncated.");
        CalibrationEntry *entry = &storage->entries[entry_index];
        memcpy(entry->name, bytes + position, 64);
        if (!entry->name[0])
            return report_error(error, error_size, "The printer calibration directory contains an unnamed entry.");
        for (size_t previous = 0; previous < entry_index; ++previous) {
            if (!strcmp(storage->entries[previous].name, entry->name))
                return report_error(error, error_size, "The printer calibration directory contains a duplicate entry.");
        }
        entry->length = read_little_endian_integer(bytes + position + 64u);
        position += 68u;
        if (entry->length > length - position)
            return report_error(error, error_size, "A printer calibration table is truncated.");
        entry->bytes = bytes + position;
        position += entry->length;
        position = (position + 3u) & ~(size_t)3u;
        if (position > length)
            return report_error(error, error_size, "The printer calibration table alignment is incomplete.");
    }
    if (position != length)
        return report_error(error, error_size, "The printer calibration file contains unexpected trailing data.");
    storage->entry_count = count;
    return 0;
}

static int load_row_tables(ProfileStorage *storage, char *error, size_t error_size)
{
    static const char *names[] = {"rows/initial-errors", "rows/noise", "rows/minimum"};
    for (size_t table = 0; table < 3; ++table) {
        const CalibrationEntry *entry = find_calibration_entry(storage, names[table], 2048);
        if (!entry)
            return report_error(error, error_size, "A required printer diffusion table is missing or invalid.");
        for (size_t word = 0; word < 1024; ++word) {
            int16_t value = read_signed_word(entry->bytes + word * 2u);
            if (table == 0)
                storage->initial_errors[word / 256u][word % 256u] = value;
            else if (table == 1)
                storage->noise[word] = value;
            else
                storage->minimum[word] = value;
        }
    }
    const CalibrationEntry *matrix = find_calibration_entry(storage, "rows/ordered-matrix", sizeof storage->ordered_matrix);
    const CalibrationEntry *rounding = find_calibration_entry(storage, "color/interpolation-rounding", BH7_COLOR_CUBE_NODES);
    if (!matrix || !rounding)
        return report_error(error, error_size, "A required printer color or dither table is missing or invalid.");
    memcpy(storage->ordered_matrix, matrix->bytes, sizeof storage->ordered_matrix);
    storage->weight_rounding = rounding->bytes;
    return 0;
}

static uint8_t clamp_rgb_value(int32_t value)
{
    return value < 0 ? 0 : value > 255 ? 255 : (uint8_t)value;
}

static void prepare_rgb_curves(ProfileStorage *storage)
{
    uint8_t brightness_curve[256], contrast_curve[256];
    const int16_t *adjustment = storage->configuration.color_adjustment;
    int32_t brightness_offset = (int32_t)adjustment[0] * 255 / 128;
    for (int32_t input = 0; input < 256; ++input) {
        brightness_curve[input] = clamp_rgb_value(input + brightness_offset);
        contrast_curve[input] = clamp_rgb_value(input + (input - 128) * adjustment[1] / 128);
    }
    for (size_t input = 0; input < 256; ++input) {
        for (size_t channel = 0; channel < 3; ++channel) {
            int32_t balanced = brightness_curve[input] +
                (int32_t)brightness_curve[input] * adjustment[channel + 2u] / 128;
            storage->rgb_curves[channel][input] = contrast_curve[clamp_rgb_value(balanced)];
        }
        storage->monochrome_curve[input] = contrast_curve[brightness_curve[input]];
    }
}

static void select_calibration_tags(const ProfileStorage *storage, unsigned color_state,
                                     uint16_t effective_mode, unsigned variant,
                                     uint32_t *cube_tag, uint32_t *secondary_tag)
{
    const Bh7PrintSettings *settings = &storage->configuration.settings;
    int16_t media = storage->configuration.internal_media;
    if ((media == 0 || media == 12) && settings->slow_drying)
        media = 8;
    unsigned quality = effective_mode & 0x0f00u;
    bool neutral = color_state == 1;
    bool matching = settings->document != BH7_CUSTOM || settings->color_match != BH7_NO_MATCH;
    unsigned stage = !matching ? 0u : variant == 1 ? 2u : 1u;
    if (media == 1 || media == 3 || media == 9 || media == 10 || media == 11 || media == 13) {
        unsigned detail = quality == 0x700u ? 6u : quality == 0x600u ? 4u : quality >= 0x500u ? 3u : 2u;
        unsigned family = media == 3 || media == 13 ? 0x100u : media == 9 ? 0x200u :
                          media == 10 ? 0x300u : media == 11 ? 0x400u : 0u;
        *cube_tag = 0x8100u + family + stage * 0x10u + detail;
        if (quality <= 0x300u && neutral)
            *cube_tag += 0x30u;
        *secondary_tag = 0xc100u + family + stage * 0x10u + detail;
    } else if (media == 4 || media == 5) {
        unsigned detail = quality <= 0x300u ? 2u : 3u;
        *cube_tag = 0x8600u + stage * 0x10u + detail;
        if (quality <= 0x300u && neutral && stage != 1u)
            *cube_tag += 0x30u;
        *secondary_tag = 0xc600u + stage * 0x10u + detail;
    } else {
        unsigned detail = quality == 0x100u ? 0u : quality == 0x200u ? 1u :
                          quality >= 0x500u ? 3u : 2u;
        unsigned family = media == 8 ? 0x700u : 0u;
        *cube_tag = 0x8000u + family + stage * 0x10u + detail;
        if (detail != 3u && neutral)
            *cube_tag += 0x30u;
        *secondary_tag = 0xc000u + family + stage * 0x10u + detail;
    }
}

static const char *external_calibration_filename(const ProfileStorage *storage)
{
    switch (storage->configuration.internal_media) {
    case 1: return "brio06aa.bcm";
    case 3: case 13: return "brio06ab.bcm";
    case 9: return "brio06ac.bcm";
    case 10: return "brio06ad.bcm";
    case 11: return "brio06ae.bcm";
    case 4: case 5: return "brio06af.bcm";
    case 8: return "brio06ag.bcm";
    default: return storage->configuration.settings.slow_drying ? "brio06ag.bcm" : "brio06a.bcm";
    }
}

static const char *select_builtin_cube(const ProfileStorage *storage, unsigned color_state,
                                        uint16_t effective_mode, unsigned variant)
{
    const Bh7PrintSettings *settings = &storage->configuration.settings;
    bool matching = settings->document != BH7_CUSTOM || settings->color_match != BH7_NO_MATCH;
    bool neutral = color_state == 1 && (effective_mode & 0x0f00u) < 0x500u;
    bool draft = (effective_mode & 0x0f00u) == 0x100u;
    if (!matching)
        return neutral ? "cube/unmatched-neutral" : "cube/unmatched-color";
    if (draft) {
        if (variant == 1)
            return neutral ? "cube/draft-natural-neutral" : "cube/draft-natural-color";
        return neutral ? "cube/draft-vivid-neutral" : "cube/draft-vivid-color";
    }
    if (variant == 1)
        return neutral ? "cube/natural-neutral" : "cube/natural-color";
    return neutral ? "cube/vivid-neutral" : "cube/vivid-color";
}

static const char *select_builtin_curves(const ProfileStorage *storage, uint16_t effective_mode,
                                         unsigned variant, uint16_t *ink_limit)
{
    bool matching = storage->configuration.settings.document != BH7_CUSTOM ||
                    storage->configuration.settings.color_match != BH7_NO_MATCH;
    bool vivid = matching && variant != 1;
    switch (effective_mode & 0x0f00u) {
    case 0x100:
        *ink_limit = 2560;
        return vivid ? "curves/builtin-draft-vivid" : "curves/builtin-draft-natural";
    case 0x200:
        *ink_limit = 1664;
        return vivid ? "curves/builtin-fast-vivid" : "curves/builtin-fast-natural";
    case 0x500: case 0x600: case 0x700:
        *ink_limit = 928;
        return vivid ? "curves/builtin-fine-vivid" : "curves/builtin-fine-natural";
    default:
        *ink_limit = 1216;
        return vivid ? "curves/builtin-normal-vivid" : "curves/builtin-normal-natural";
    }
}

static int prepare_profile_variant(ProfileStorage *storage, PreparedProfile *prepared,
                                    unsigned color_state, uint16_t effective_mode,
                                    char *error, size_t error_size)
{
    const Bh7PrintSettings *settings = &storage->configuration.settings;
    unsigned variant = settings->document == BH7_DOCUMENT_PHOTO ||
        (settings->document == BH7_CUSTOM && settings->color_match != BH7_VIVID) ? 1u : 2u;
    uint32_t cube_tag, secondary_tag;
    select_calibration_tags(storage, color_state, effective_mode, variant, &cube_tag, &secondary_tag);
    const char *filename = external_calibration_filename(storage);
    char *path = data_path(storage, filename);
    Bh7CalibrationData *external = malloc(sizeof *external);
    if (!path || !external) {
        free(path);
        free(external);
        return report_error(error, error_size, "There is not enough memory to prepare the printer color profile.");
    }
    const CalibrationEntry *curves = NULL;
    char load_error[256];
    int loaded_external = bh7_calibration_load(path, cube_tag, secondary_tag, external, load_error, sizeof load_error) == 0;
    free(path);
    if (loaded_external) {
        char key[64];
        size_t filename_length = strlen(filename);
        (void)snprintf(key, sizeof key, "curves/%.*s/%04x", (int)(filename_length - 4u), filename, secondary_tag);
        curves = find_calibration_entry(storage, key, PREPARED_CURVE_BYTES);
        if (!curves) {
            free(external);
            return report_error(error, error_size, "The selected Brother color profile has no prepared correction tables.");
        }
        for (size_t word = 0; word < BH7_SECONDARY_WORDS; ++word) {
            if (external->secondary[word] != read_little_endian_word(curves->bytes + word * 2u)) {
                free(external);
                return report_error(error, error_size, "The Brother calibration curves do not match this software version.");
            }
        }
        memcpy(prepared->cube, external->cube, sizeof prepared->cube);
        prepared->ink_limit = external->ink_limit;
    } else {
        const CalibrationEntry *cube = find_calibration_entry(storage,
            select_builtin_cube(storage, color_state, effective_mode, variant), BH7_COLOR_CUBE_WORDS * 2u);
        curves = find_calibration_entry(storage,
            select_builtin_curves(storage, effective_mode, variant, &prepared->ink_limit), PREPARED_CURVE_BYTES);
        if (!cube || !curves) {
            free(external);
            return report_error(error, error_size, "A built-in printer color profile is missing or invalid.");
        }
        for (size_t word = 0; word < BH7_COLOR_CUBE_WORDS; ++word)
            prepared->cube[word] = read_little_endian_word(cube->bytes + word * 2u);
    }
    free(external);
    bool direct_monochrome = settings->monochrome && (effective_mode & 0x5000u) != 0x5000u;
    const uint8_t *tone = curves->bytes + BH7_SECONDARY_WORDS * 2u;
    const uint8_t *correction = tone + 8192u;
    const uint8_t *direct = correction + 8192u;
    if (!direct_monochrome) {
        for (size_t channel = 0; channel < 4; ++channel) {
            for (size_t input = 0; input < BH7_COLOR_CURVE_LENGTH; ++input) {
                size_t position = (channel * BH7_COLOR_CURVE_LENGTH + input) * 2u;
                prepared->tone_curves[channel][input] = read_little_endian_word(tone + position);
                prepared->black_correction[channel][input] = read_signed_word(correction + position);
            }
        }
    }
    for (size_t input = 0; input < 256; ++input)
        prepared->direct_black_tone[input] = read_little_endian_word(direct + input * 2u);
    prepared->color_state = color_state;
    prepared->effective_mode = effective_mode;
    return 0;
}

int bh7_profile_select(Bh7Profile *profile, unsigned color_state,
                       uint16_t effective_mode, char *error, size_t error_size)
{
    if (!profile || !profile->calibration_storage || (color_state != 1 && color_state != 2))
        return report_error(error, error_size, "The requested printer color-processing mode is invalid.");
    ProfileStorage *storage = profile->calibration_storage;
    PreparedProfile *prepared = NULL;
    for (size_t cached = 0; cached < MAXIMUM_CACHED_PROFILES; ++cached) {
        PreparedProfile *candidate = storage->cached_profiles[cached];
        if (candidate && candidate->color_state == color_state && candidate->effective_mode == effective_mode) {
            prepared = candidate;
            break;
        }
    }
    if (!prepared) {
        prepared = calloc(1, sizeof *prepared);
        if (!prepared)
            return report_error(error, error_size, "There is not enough memory to cache the printer color profile.");
        if (prepare_profile_variant(storage, prepared, color_state, effective_mode, error, error_size)) {
            free(prepared);
            return -1;
        }
        size_t slot = storage->next_cache_slot++ % MAXIMUM_CACHED_PROFILES;
        free(storage->cached_profiles[slot]);
        storage->cached_profiles[slot] = prepared;
    }
    profile->color.cube = prepared->cube;
    profile->color.rgb_curves = (const uint8_t (*)[256])storage->rgb_curves;
    profile->color.monochrome_curve = storage->monochrome_curve;
    profile->color.weight_rounding = storage->weight_rounding;
    profile->color.monochrome = storage->configuration.settings.monochrome;
    profile->color.direct_monochrome = storage->configuration.settings.monochrome &&
                                     (effective_mode & 0x5000u) != 0x5000u;
    profile->color.neutral_balance = color_state == 1;
    profile->finish.tone_curves = (const uint16_t (*)[BH7_COLOR_CURVE_LENGTH])prepared->tone_curves;
    profile->finish.black_correction = (const int16_t (*)[BH7_COLOR_CURVE_LENGTH])prepared->black_correction;
    profile->finish.ink_limit = prepared->ink_limit;
    profile->rows.initial_errors = (const int16_t (*)[256])storage->initial_errors;
    profile->rows.noise = storage->noise;
    profile->rows.minimum = storage->minimum;
    profile->rows.ordered_matrix = storage->ordered_matrix;
    profile->direct_black_tone = prepared->direct_black_tone;
    if (error && error_size)
        error[0] = '\0';
    return 0;
}

Bh7Profile *bh7_profile_create(const Bh7DeviceConfig *configuration,
                               const char *data_directory, char *error, size_t error_size)
{
    if (!configuration || !data_directory || !data_directory[0]) {
        (void)report_error(error, error_size, "The printer configuration or calibration directory is missing.");
        return NULL;
    }
    Bh7Profile *profile = calloc(1, sizeof *profile);
    ProfileStorage *storage = calloc(1, sizeof *storage);
    if (!profile || !storage) {
        free(profile);
        free(storage);
        (void)report_error(error, error_size, "There is not enough memory to create the printer color profile.");
        return NULL;
    }
    profile->calibration_storage = storage;
    storage->configuration = *configuration;
    storage->data_directory = strdup(data_directory);
    if (!storage->data_directory) {
        (void)report_error(error, error_size, "There is not enough memory to retain the printer calibration directory.");
        bh7_profile_destroy(profile);
        return NULL;
    }
    if (load_calibration_pack(storage, error, error_size) ||
        load_row_tables(storage, error, error_size)) {
        bh7_profile_destroy(profile);
        return NULL;
    }
    prepare_rgb_curves(storage);
    if (bh7_profile_select(profile, configuration->render_class, configuration->effective_mode_word, error, error_size)) {
        bh7_profile_destroy(profile);
        return NULL;
    }
    return profile;
}

void bh7_profile_destroy(Bh7Profile *profile)
{
    if (!profile)
        return;
    ProfileStorage *storage = profile->calibration_storage;
    if (storage) {
        for (size_t cached = 0; cached < MAXIMUM_CACHED_PROFILES; ++cached)
            free(storage->cached_profiles[cached]);
        free(storage->calibration_bytes);
        free(storage->data_directory);
        free(storage);
    }
    free(profile);
}
