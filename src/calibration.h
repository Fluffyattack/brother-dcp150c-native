/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef BH7_CALIBRATION_H
#define BH7_CALIBRATION_H

#include <stddef.h>
#include <stdint.h>

#include "color.h"

#define BH7_SECONDARY_WORDS 2304u

/* Decoded printer calibration data. File ownership/licensing stays separate. */
typedef struct {
    uint16_t cube[BH7_COLOR_CUBE_WORDS];
    uint16_t secondary[BH7_SECONDARY_WORDS];
    uint16_t ink_limit;
} Bh7CalibrationData;

/* Load both selected entries atomically; the output is unchanged on failure. */
int bh7_calibration_load(const char *path, uint32_t cube_tag,
                         uint32_t secondary_tag, Bh7CalibrationData *output,
                         char *error, size_t error_size);

#endif
