/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef DCP150C_CODECS_H
#define DCP150C_CODECS_H

#include <stddef.h>
#include <stdint.h>

enum dcp150c_codec_result {
    DCP150C_CODEC_OK = 0,
    DCP150C_CODEC_INVALID = -1,
    DCP150C_CODEC_NO_SPACE = -2,
    DCP150C_CODEC_OVERFLOW = -3
};

/*
 * Payload encoders use disjoint input/output buffers. A NULL destination with
 * zero capacity measures the exact encoded size. On insufficient capacity,
 * *written reports the required size and the destination is left unchanged.
 * Empty input produces an empty payload. Lengths must fit the wire format:
 * at most 65535 input/output bytes. Forward PackBits output, reverse PackBits
 * input, and quad input are limited to 32767 bytes to exclude legacy signed
 * offset overflows.
 */
int dcp150c_packbits_encode(const uint8_t *source, size_t length,
                           uint8_t *destination, size_t capacity, size_t *written);

/* Reverse byte order and reverse the bits of every input byte before coding.
 * One-byte input is rejected because that legacy case has no defined result.
 */
int dcp150c_packbits_reverse_encode(const uint8_t *source, size_t length,
                                   uint8_t *destination, size_t capacity, size_t *written);

/* Mixed byte/16-bit/32-bit repetition coding for four-byte raster groups. */
int dcp150c_quad_encode(const uint8_t *source, size_t length,
                       uint8_t *destination, size_t capacity, size_t *written);

/* Sparse zero/literal groups. *flags is 1, or 5 when fewer than three groups
 * contain literal data. Preserve this value when framing the encoded payload.
 */
int dcp150c_sparse_encode(const uint8_t *source, size_t length,
                         uint8_t *destination, size_t capacity, size_t *written,
                         unsigned *flags);

/* Reverse raster byte order and the order of 1-bit or 2-bit pixels per byte.
 * Source and destination must be disjoint. Returns the codec result code.
 */
int dcp150c_reverse_pixels(const uint8_t *source, size_t length,
                          unsigned bits_per_pixel, uint8_t *destination, size_t capacity);

/* Wrap an existing payload in a G + little-endian uint16 length header.
 * flags is 0 for ordinary payloads or the sparse encoder's result. Bit 2 sets
 * the header's high bit, requiring payload length <=32767. Overlap is allowed.
 */
int dcp150c_frame_block(const uint8_t *payload, size_t length, unsigned flags,
                       uint8_t *destination, size_t capacity, size_t *written);

#endif
