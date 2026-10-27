/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef BH7_INPUT_H
#define BH7_INPUT_H

#include "device.h"

typedef struct {
    Bh7PrintSettings settings;
    char paper_name[32];
    uint16_t raster_dpi;
} Bh7InputConfiguration;

/* Output objects are replaced only after the complete file is validated. */
int bh7_read_printer_configuration(const char *path,
                                    Bh7InputConfiguration *configuration,
                                    char *error, size_t error_capacity);
int bh7_read_paper_dimensions(const char *path, const char *paper_name,
                               uint32_t *width_600, uint32_t *height_600,
                               char *error, size_t error_capacity);

#endif
