/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Portable raster payload coding for the Brother DCP-150C. */
#include "codecs.h"

#include <stdbool.h>
#include <string.h>

struct output {
    uint8_t *bytes;
    size_t length;
};

static void append(struct output *output, uint8_t value)
{
    if (output->bytes != NULL)
        output->bytes[output->length] = value;
    output->length++;
}

static void patch(struct output *output, size_t offset, uint8_t value)
{
    if (output->bytes != NULL)
        output->bytes[offset] = value;
}

static void append_bytes(struct output *output, const uint8_t *source, size_t length)
{
    if (length != 0 && output->bytes != NULL)
        memcpy(output->bytes + output->length, source, length);
    output->length += length;
}

static uint8_t reverse_bits(uint8_t value)
{
    value = (uint8_t)((value >> 4) | (value << 4));
    value = (uint8_t)(((value & 0xccU) >> 2) | ((value & 0x33U) << 2));
    return (uint8_t)(((value & 0xaaU) >> 1) | ((value & 0x55U) << 1));
}

static uint8_t reverse_pairs(uint8_t value)
{
    return (uint8_t)(((value & 0x03U) << 6) | ((value & 0x0cU) << 2) |
                     ((value & 0x30U) >> 2) | ((value & 0xc0U) >> 6));
}

static uint8_t input_byte(const uint8_t *source, size_t length, size_t offset, bool reverse)
{
    return reverse ? reverse_bits(source[length - offset - 1]) : source[offset];
}

static bool overlaps(const void *left, size_t left_length, const void *right, size_t right_length)
{
    uintptr_t a = (uintptr_t)left;
    uintptr_t b = (uintptr_t)right;
    if (left_length == 0 || right_length == 0)
        return false;
    return a <= b ? b - a < left_length : a - b < right_length;
}

static int validate(const uint8_t *source, size_t length, uint8_t *destination,
                    size_t capacity, size_t *written, size_t maximum)
{
    if (written == NULL)
        return DCP150C_CODEC_INVALID;
    *written = 0;
    if ((source == NULL && length != 0) || (destination == NULL && capacity != 0))
        return DCP150C_CODEC_INVALID;
    if (length > maximum)
        return DCP150C_CODEC_OVERFLOW;
    if (destination != NULL && overlaps(source, length, destination, capacity))
        return DCP150C_CODEC_INVALID;
    return DCP150C_CODEC_OK;
}

/* Keep the printer's literal/run split policy, including its two-byte tail. */
static size_t packbits(const uint8_t *source, size_t length, uint8_t *destination, bool reverse)
{
    struct output output = {destination, 0};
    size_t literal_header = 0;
    size_t literals = 0;
    size_t repetitions = 0;
    bool in_run = false;

    if (length == 0)
        return 0;
    if (length < 3) {
        append(&output, (uint8_t)(length - 1));
        for (size_t index = 0; index < length; index++)
            append(&output, input_byte(source, length, index, reverse));
        return output.length;
    }

    for (size_t cursor = 1; cursor < length - 1; cursor++) {
        uint8_t previous = input_byte(source, length, cursor - 1, reverse);
        uint8_t current = input_byte(source, length, cursor, reverse);
        if (in_run) {
            if (current == previous && repetitions < 128) {
                repetitions++;
            } else {
                append(&output, (uint8_t)(1 - repetitions));
                append(&output, previous);
                literal_header = output.length;
                append(&output, 0);
                literals = 0;
                in_run = false;
            }
        } else if (previous == current &&
                   previous == input_byte(source, length, cursor + 1, reverse)) {
            if (literals == 0) {
                if (repetitions != 0)
                    output.length--;
            } else {
                patch(&output, literal_header, (uint8_t)(literals - 1));
            }
            in_run = true;
            repetitions = 2;
        } else {
            if (repetitions == 0 && literals == 0)
                append(&output, 0);
            if (literals > 126) {
                patch(&output, literal_header, (uint8_t)(literals - 1));
                literals = 0;
                literal_header = output.length;
                append(&output, 0);
            }
            literals++;
            append(&output, previous);
        }
    }

    uint8_t previous = input_byte(source, length, length - 2, reverse);
    uint8_t last = input_byte(source, length, length - 1, reverse);
    if (in_run) {
        if (last != previous || repetitions > 127) {
            append(&output, (uint8_t)(1 - repetitions));
            append(&output, previous);
            append(&output, 0);
            append(&output, last);
        } else {
            append(&output, (uint8_t)(0 - repetitions));
            append(&output, previous);
        }
    } else {
        if (literals < 126) {
            patch(&output, literal_header, (uint8_t)(literals + 1));
        } else {
            patch(&output, literal_header, (uint8_t)(literals - 1));
            append(&output, 1);
        }
        append(&output, previous);
        append(&output, last);
    }
    return output.length;
}

static uint32_t read_u32le(const uint8_t *source)
{
    return (uint32_t)source[0] | ((uint32_t)source[1] << 8) |
           ((uint32_t)source[2] << 16) | ((uint32_t)source[3] << 24);
}

static void append_word(struct output *output, uint32_t word, unsigned width)
{
    for (unsigned byte = 0; byte < width; byte++)
        append(output, (uint8_t)(word >> (byte * 8)));
}

static void repeat_words(struct output *output, uint32_t word, size_t words)
{
    if ((uint16_t)word == (uint16_t)(word >> 16)) {
        if ((uint8_t)word == (uint8_t)(word >> 8)) {
            size_t bytes = words * 4;
            while (bytes != 0) {
                if (bytes >= 128) {
                    append(output, 0xff);
                    append_word(output, word, 4);
                    bytes -= 128;
                } else if (bytes >= 64) {
                    append(output, 0xfd);
                    append_word(output, word, 2);
                    bytes -= 64;
                } else if (bytes >= 32) {
                    append(output, 0xfc);
                    append(output, (uint8_t)word);
                    bytes -= 32;
                } else {
                    append(output, (uint8_t)(((bytes - 1) << 2) | 0x80));
                    append(output, (uint8_t)word);
                    bytes = 0;
                }
            }
        } else {
            size_t halves = words * 2;
            while (halves != 0) {
                if (halves >= 64) {
                    append(output, 0xff);
                    append_word(output, word, 4);
                    halves -= 64;
                } else {
                    size_t count = halves > 32 ? 32 : halves;
                    append(output, (uint8_t)(((count - 1) << 2) | 0x81));
                    append_word(output, word, 2);
                    halves -= count;
                }
            }
        }
    } else {
        while (words != 0) {
            size_t count = words > 32 ? 32 : words;
            append(output, (uint8_t)(((count - 1) << 2) | 0x83));
            append_word(output, word, 4);
            words -= count;
        }
    }
}

static void quad_bytes(struct output *output, const uint8_t *source, size_t start, size_t end)
{
    size_t cursor = start;
    while (cursor < end) {
        size_t run = 1;
        while (cursor + run < end && source[cursor + run] == source[cursor])
            run++;
        if (run >= 2) {
            append(output, (uint8_t)(((run - 1) << 2) | 0x80));
            append(output, source[cursor]);
            cursor += run;
        } else {
            size_t beginning = cursor++;
            while (cursor < end) {
                if (cursor + 1 < end && source[cursor] == source[cursor + 1])
                    break;
                cursor++;
            }
            while (beginning < cursor) {
                size_t count = cursor - beginning;
                if (count > 128)
                    count = 128;
                append(output, (uint8_t)(count - 1));
                append_bytes(output, source + beginning, count);
                beginning += count;
            }
        }
    }
}

static size_t quad(const uint8_t *source, size_t length, uint8_t *destination)
{
    struct output output = {destination, 0};
    size_t remaining = length;
    size_t position = 0;
    while (remaining != 0) {
        if (remaining < 9) {
            quad_bytes(&output, source, position, length);
            break;
        }
        uint32_t first = read_u32le(source + position);
        uint32_t second = read_u32le(source + position + 4);
        size_t next = position + 8;
        if (first == second) {
            size_t count = 2;
            remaining -= 8;
            position = next;
            while (remaining > 4 && read_u32le(source + position) == second) {
                remaining -= 4;
                position += 4;
                count++;
            }
            repeat_words(&output, first, count);
        } else {
            size_t scan_remaining = remaining - 4;
            size_t last = next;
            size_t literal_end = position;
            if (scan_remaining > 8) {
                do {
                    last = next;
                    uint32_t current = read_u32le(source + last);
                    if (second == current)
                        break;
                    scan_remaining -= 4;
                    second = current;
                    next = last + 4;
                } while (scan_remaining > 8);
                literal_end = last - 4;
                last += 4;
            }
            quad_bytes(&output, source, position, literal_end);
            position = last - 8;
            remaining = scan_remaining;
        }
    }
    return output.length;
}

static size_t sparse(const uint8_t *source, size_t length, uint8_t *destination, unsigned *flags)
{
    struct output output = {destination, 0};
    size_t zeros = 0;
    size_t literals = 0;
    size_t nonempty_groups = 0;
    for (size_t cursor = 0; cursor < length; cursor++) {
        bool end_of_literals = false;
        if (source[cursor] == 0) {
            zeros++;
        } else {
            literals++;
            end_of_literals = cursor + 1 < length && source[cursor + 1] == 0;
        }
        if (end_of_literals || zeros > 30 || literals > 6 || cursor + 1 == length) {
            append(&output, (uint8_t)((literals << 5) | zeros));
            if (literals != 0) {
                append_bytes(&output, source + cursor + 1 - literals, literals);
                nonempty_groups++;
            }
            zeros = 0;
            literals = 0;
        }
    }
    *flags = nonempty_groups < 3 ? 5U : 1U;
    return output.length;
}

static int encode_packbits(const uint8_t *source, size_t length, uint8_t *destination,
                          size_t capacity, size_t *written, bool reverse)
{
    int result = validate(source, length, destination, capacity, written,
                          reverse ? INT16_MAX : UINT16_MAX);
    if (result != DCP150C_CODEC_OK)
        return result;
    if (reverse && length == 1)
        return DCP150C_CODEC_INVALID;
    size_t needed = packbits(source, length, NULL, reverse);
    *written = needed;
    if (needed > (reverse ? UINT16_MAX : INT16_MAX))
        return DCP150C_CODEC_OVERFLOW;
    if (destination == NULL)
        return DCP150C_CODEC_OK;
    if (capacity < needed)
        return DCP150C_CODEC_NO_SPACE;
    (void)packbits(source, length, destination, reverse);
    return DCP150C_CODEC_OK;
}

int dcp150c_packbits_encode(const uint8_t *source, size_t length,
                           uint8_t *destination, size_t capacity, size_t *written)
{
    return encode_packbits(source, length, destination, capacity, written, false);
}

int dcp150c_packbits_reverse_encode(const uint8_t *source, size_t length,
                                   uint8_t *destination, size_t capacity, size_t *written)
{
    return encode_packbits(source, length, destination, capacity, written, true);
}

int dcp150c_quad_encode(const uint8_t *source, size_t length,
                       uint8_t *destination, size_t capacity, size_t *written)
{
    int result = validate(source, length, destination, capacity, written, INT16_MAX);
    if (result != DCP150C_CODEC_OK)
        return result;
    size_t needed = quad(source, length, NULL);
    *written = needed;
    if (needed > UINT16_MAX)
        return DCP150C_CODEC_OVERFLOW;
    if (destination == NULL)
        return DCP150C_CODEC_OK;
    if (capacity < needed)
        return DCP150C_CODEC_NO_SPACE;
    (void)quad(source, length, destination);
    return DCP150C_CODEC_OK;
}

int dcp150c_sparse_encode(const uint8_t *source, size_t length,
                         uint8_t *destination, size_t capacity, size_t *written, unsigned *flags)
{
    int result = validate(source, length, destination, capacity, written, UINT16_MAX);
    if (flags == NULL)
        return DCP150C_CODEC_INVALID;
    *flags = 0;
    if (result != DCP150C_CODEC_OK)
        return result;
    unsigned encoded_flags;
    size_t needed = sparse(source, length, NULL, &encoded_flags);
    *written = needed;
    if (needed > UINT16_MAX)
        return DCP150C_CODEC_OVERFLOW;
    *flags = encoded_flags;
    if (destination == NULL)
        return DCP150C_CODEC_OK;
    if (capacity < needed)
        return DCP150C_CODEC_NO_SPACE;
    (void)sparse(source, length, destination, flags);
    return DCP150C_CODEC_OK;
}

int dcp150c_reverse_pixels(const uint8_t *source, size_t length, unsigned bits_per_pixel,
                          uint8_t *destination, size_t capacity)
{
    size_t ignored;
    int result = validate(source, length, destination, capacity, &ignored, UINT16_MAX);
    if (result != DCP150C_CODEC_OK)
        return result;
    if (bits_per_pixel != 1 && bits_per_pixel != 2)
        return DCP150C_CODEC_INVALID;
    if (destination == NULL)
        return length == 0 ? DCP150C_CODEC_OK : DCP150C_CODEC_INVALID;
    if (capacity < length)
        return DCP150C_CODEC_NO_SPACE;
    for (size_t cursor = 0; cursor < length; cursor++) {
        uint8_t value = source[length - cursor - 1];
        destination[cursor] = bits_per_pixel == 1 ? reverse_bits(value) : reverse_pairs(value);
    }
    return DCP150C_CODEC_OK;
}

int dcp150c_frame_block(const uint8_t *payload, size_t length, unsigned flags,
                       uint8_t *destination, size_t capacity, size_t *written)
{
    if (written == NULL)
        return DCP150C_CODEC_INVALID;
    *written = 0;
    if ((payload == NULL && length != 0) || (destination == NULL && capacity != 0) ||
        (flags & ~5U) != 0)
        return DCP150C_CODEC_INVALID;
    if (length > UINT16_MAX || ((flags & 4U) != 0 && length > INT16_MAX))
        return DCP150C_CODEC_OVERFLOW;
    *written = length + 3;
    if (destination == NULL)
        return DCP150C_CODEC_OK;
    if (capacity < length + 3)
        return DCP150C_CODEC_NO_SPACE;
    if (length != 0)
        memmove(destination + 3, payload, length);
    destination[0] = 'G';
    destination[1] = (uint8_t)length;
    destination[2] = (uint8_t)((length >> 8) | ((flags & 4U) != 0 ? 0x80U : 0U));
    return DCP150C_CODEC_OK;
}
