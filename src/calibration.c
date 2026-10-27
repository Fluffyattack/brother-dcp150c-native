/* SPDX-License-Identifier: GPL-2.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include "calibration.h"

#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define MAX_CALIBRATION_SIZE (64u * 1024u * 1024u)
#define DIRECTORY_OFFSET 128u
#define ENTRY_HEADER_SIZE 32u

static uint16_t little16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | (uint16_t)((uint16_t)p[1] << 8));
}

static uint32_t big32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
         | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static int failure(char *message, size_t capacity, const char *format, ...)
{
    if (message && capacity) {
        va_list args;
        va_start(args, format);
        (void)vsnprintf(message, capacity, format, args);
        va_end(args);
    }
    return -1;
}

static const uint8_t *find_entry(const uint8_t *bytes, size_t size,
                                  uint32_t count, uint32_t tag,
                                  size_t expected_size)
{
    for (uint32_t i = 0; i < count; ++i) {
        const uint8_t *entry = bytes + DIRECTORY_OFFSET + 4u + (size_t)i * 12u;
        if (big32(entry) != tag)
            continue;
        size_t offset = (size_t)big32(entry + 4);
        size_t length = (size_t)big32(entry + 8);
        if (length != expected_size || offset > size || length > size - offset)
            return NULL;
        return bytes + offset;
    }
    return NULL;
}

int bh7_calibration_load(const char *path, uint32_t cube_tag,
                         uint32_t secondary_tag, Bh7CalibrationData *output,
                         char *error, size_t error_size)
{
    if (!path || !output)
        return failure(error, error_size, "Missing calibration path or output");
    FILE *file = fopen(path, "rb");
    if (!file)
        return failure(error, error_size, "Cannot open calibration: %s", strerror(errno));
    struct stat info;
    if (fstat(fileno(file), &info) || !S_ISREG(info.st_mode)
        || info.st_size < (off_t)(DIRECTORY_OFFSET + 4u)
        || info.st_size > (off_t)MAX_CALIBRATION_SIZE) {
        (void)fclose(file);
        return failure(error, error_size, "Invalid calibration file size or type");
    }
    size_t size = (size_t)info.st_size;
    uint8_t *bytes = malloc(size);
    Bh7CalibrationData *decoded = malloc(sizeof(*decoded));
    if (!bytes || !decoded) {
        free(bytes);
        free(decoded);
        (void)fclose(file);
        return failure(error, error_size, "Cannot allocate calibration memory");
    }
    int result = -1;
    if (fread(bytes, 1, size, file) != size || ferror(file)) {
        (void)failure(error, error_size, "Cannot read complete calibration file");
        goto done;
    }
    if (big32(bytes) != size || little16(bytes + 8) != 2u) {
        (void)failure(error, error_size, "Unsupported calibration header");
        goto done;
    }
    uint32_t count = big32(bytes + DIRECTORY_OFFSET);
    if ((size_t)count > (size - DIRECTORY_OFFSET - 4u) / 12u) {
        (void)failure(error, error_size, "Truncated calibration directory");
        goto done;
    }
    const uint8_t *cube = find_entry(bytes, size, count, cube_tag,
                                     ENTRY_HEADER_SIZE + BH7_COLOR_CUBE_WORDS * 2u);
    const uint8_t *secondary = find_entry(bytes, size, count, secondary_tag,
                                          ENTRY_HEADER_SIZE + BH7_SECONDARY_WORDS * 2u);
    if (!cube || !secondary) {
        (void)failure(error, error_size, "Required calibration entries are absent or invalid");
        goto done;
    }
    for (size_t i = 0; i < BH7_COLOR_CUBE_WORDS; ++i)
        decoded->cube[i] = little16(cube + ENTRY_HEADER_SIZE + i * 2u);
    for (size_t i = 0; i < BH7_SECONDARY_WORDS; ++i)
        decoded->secondary[i] = little16(secondary + ENTRY_HEADER_SIZE + i * 2u);
    decoded->ink_limit = little16(secondary + 12u);
    memcpy(output, decoded, sizeof(*output));
    if (error && error_size)
        error[0] = '\0';
    result = 0;
done:
    free(decoded);
    free(bytes);
    (void)fclose(file);
    return result;
}
