/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef BH7_PROFILE_H
#define BH7_PROFILE_H

#include "device.h"
#include "rows.h"
#include <stddef.h>
#include <stdint.h>

/* Ready-to-use views into calibration data owned by this profile. */
typedef struct {
    Bh7ColorProfile color;
    Bh7ColorFinishProfile finish;
    Bh7RowsTables rows;
    const uint16_t *direct_black_tone; /* 256 entries for the current selection. */
    void *calibration_storage;
} Bh7Profile;

Bh7Profile *bh7_profile_create(const Bh7DeviceConfig *configuration,
                               const char *data_directory,
                               char *error, size_t error_size);

/* Select the current band's profile without resetting its diffusion state. */
int bh7_profile_select(Bh7Profile *profile, unsigned color_state,
                       uint16_t effective_mode,
                       char *error, size_t error_size);

void bh7_profile_destroy(Bh7Profile *profile);

#endif
