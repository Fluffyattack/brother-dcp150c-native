/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "device.h"
#include "input.h"
#include "pipeline.h"
#include "profile.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef DCP150C_PRODUCTION_READY
#define DCP150C_PRODUCTION_READY 0
#endif

#define DEFAULT_DATA_DIRECTORY "/usr/share/dcp150c-native"
#define DEFAULT_CONFIGURATION "/etc/dcp150c-native/default.conf"

static void describe_usage(const char *program) {
    printf("Usage: %s [-rc CONFIGURATION] [-pi PAPER_INFORMATION] [INPUT.ppm]\n\n", program);
    puts("Convert complete RGB portable pixmap pages into Brother DCP-150C printer data.\n"
         "When no input file is supplied, pages are read from standard input.\n"
         "Printer data is written to standard output.\n\n"
         "Options:\n"
         "  -rc, --config FILE       Printer settings file\n"
         "  -pi, --paper-info FILE   Paper dimensions file\n"
         "  --data-dir DIRECTORY     Installed printer calibration directory\n"
         "  --capabilities           Describe this build as JSON\n"
         "  --version                Show the software version\n"
         "  -h, --help               Show this help\n\n"
         "DCP150C_CONFIG and DCP150C_DATA_DIR override the default installation paths.\n"
         "The page dimensions must match the selected paper and print quality.\n"
         "Draft uses 150 dpi, Fast uses 300 dpi, and the other qualities use 600 dpi.");
}

static const char *environment_or_default(const char *name, const char *fallback) {
    const char *value = getenv(name);
    return value && *value ? value : fallback;
}

static char *default_paper_information_path(const char *directory) {
    const char suffix[] = "/paperinf";
    size_t directory_length = strlen(directory);
    if (directory_length > SIZE_MAX - sizeof(suffix)) return NULL;
    char *path = malloc(directory_length + sizeof(suffix));
    if (!path) return NULL;
    memcpy(path, directory, directory_length);
    memcpy(path + directory_length, suffix, sizeof(suffix));
    return path;
}

static int select_raster_profile(void *opaque, unsigned rendering_class,
                                  uint16_t effective_mode_word,
                                  Bh7RasterProfile *selected_profile,
                                  char *error, size_t error_capacity) {
    Bh7Profile *profile = opaque;
    if (bh7_profile_select(profile, rendering_class, effective_mode_word, error, error_capacity) < 0) return -1;
    selected_profile->color = profile->color;
    selected_profile->finish = profile->finish;
    selected_profile->rows = profile->rows;
    selected_profile->direct_black_tone = profile->direct_black_tone;
    return 0;
}

int main(int argument_count, char **arguments) {
    const char *configuration_path = environment_or_default("DCP150C_CONFIG", DEFAULT_CONFIGURATION);
    const char *data_directory = environment_or_default("DCP150C_DATA_DIR", DEFAULT_DATA_DIRECTORY);
    const char *paper_information_path = NULL;
    const char *input_path = NULL;
    bool help_requested = false, capabilities_requested = false, version_requested = false;
    bool positional_only = false;
    for (int argument_index = 1; argument_index < argument_count; ++argument_index) {
        const char *option = arguments[argument_index];
        if (!positional_only && !strcmp(option, "--")) { positional_only = true; continue; }
        if (!positional_only && (!strcmp(option, "-h") || !strcmp(option, "--help"))) { help_requested = true; continue; }
        if (!positional_only && !strcmp(option, "--capabilities")) { capabilities_requested = true; continue; }
        if (!positional_only && !strcmp(option, "--version")) { version_requested = true; continue; }
        if (!positional_only && (!strcmp(option, "-rc") || !strcmp(option, "--config") ||
            !strcmp(option, "-pi") || !strcmp(option, "--paper-info") || !strcmp(option, "--data-dir"))) {
            if (++argument_index == argument_count || !*arguments[argument_index]) {
                fprintf(stderr, "ERROR: The option %s requires a file or directory path.\n", option);
                return 2;
            }
            if (!strcmp(option, "-rc") || !strcmp(option, "--config")) configuration_path = arguments[argument_index];
            else if (!strcmp(option, "-pi") || !strcmp(option, "--paper-info")) paper_information_path = arguments[argument_index];
            else data_directory = arguments[argument_index];
            continue;
        }
        if (!positional_only && option[0] == '-') {
            fprintf(stderr, "ERROR: The option %s is not supported. Use --help to see the available options.\n", option);
            return 2;
        }
        if (input_path) {
            fputs("ERROR: Supply one input file, or concatenate multiple portable pixmap pages on standard input.\n", stderr);
            return 2;
        }
        input_path = option;
    }
    if (help_requested) { describe_usage(arguments[0]); return ferror(stdout) ? 1 : 0; }
    if (version_requested) { puts("Brother DCP-150C Native 0.1.0"); return ferror(stdout) ? 1 : 0; }
    if (capabilities_requested) {
        printf("{\"model\":\"Brother DCP-150C\",\"version\":\"0.1.0\",\"production_ready\":%s,"
               "\"native_raster\":true,\"input_format\":\"P6 RGB, eight-bit channels\","
               "\"qualities\":[\"Draft\",\"Fast\",\"Normal\",\"Fine\",\"Photo\",\"Highest\"]}\n",
               DCP150C_PRODUCTION_READY ? "true" : "false");
        return ferror(stdout) ? 1 : 0;
    }

    char error[512];
    Bh7InputConfiguration input_configuration;
    if (bh7_read_printer_configuration(configuration_path, &input_configuration, error, sizeof(error)) < 0) {
        fprintf(stderr, "ERROR: %s\n", error);
        return 1;
    }
    char *allocated_paper_path = NULL;
    if (!paper_information_path) {
        allocated_paper_path = default_paper_information_path(data_directory);
        if (!allocated_paper_path) {
            fputs("ERROR: There is not enough memory to locate the installed paper information.\n", stderr);
            return 1;
        }
        paper_information_path = allocated_paper_path;
    }
    uint32_t paper_width_600, paper_height_600;
    if (bh7_read_paper_dimensions(paper_information_path, input_configuration.paper_name,
                                  &paper_width_600, &paper_height_600, error, sizeof(error)) < 0) {
        fprintf(stderr, "ERROR: %s\n", error);
        free(allocated_paper_path);
        return 1;
    }
    free(allocated_paper_path);
    Bh7DeviceConfig device;
    if (bh7_device_configure(&device, &input_configuration.settings, paper_width_600, paper_height_600) < 0) {
        fputs("ERROR: The selected printer settings cannot be converted into a valid page configuration.\n", stderr);
        return 1;
    }
    Bh7Profile *profile = bh7_profile_create(&device, data_directory, error, sizeof(error));
    if (!profile) {
        fprintf(stderr, "ERROR: %s\n", error);
        return 1;
    }
    FILE *input = input_path ? fopen(input_path, "rb") : stdin;
    if (!input) {
        fputs("ERROR: The input portable pixmap file could not be opened.\n", stderr);
        bh7_profile_destroy(profile);
        return 1;
    }
    Bh7RasterProfileProvider profiles = {.select = select_raster_profile, .opaque = profile};
    size_t rendered_page_count = 0;
    int result = bh7_pipeline_render_ppm(input, stdout, &device, &profiles,
                                         &rendered_page_count, error, sizeof(error));
    if (input != stdin) fclose(input);
    bh7_profile_destroy(profile);
    if (result < 0) {
        fprintf(stderr, "ERROR: %s\n", error);
        return 1;
    }
    fprintf(stderr, "INFO: Rendered %zu %s for the Brother DCP-150C.\n", rendered_page_count,
            rendered_page_count == 1 ? "page" : "pages");
    return 0;
}
