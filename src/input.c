/* SPDX-License-Identifier: GPL-2.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include "input.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <unistd.h>

enum { MAXIMUM_SETTINGS_BYTES = 1024 * 1024 };

static int explain_failure(char *error, size_t capacity, const char *format, ...) {
    if (error && capacity) {
        va_list arguments;
        va_start(arguments, format);
        vsnprintf(error, capacity, format, arguments);
        va_end(arguments);
    }
    return -1;
}

static char *trim_whitespace(char *text) {
    while (*text && isspace((unsigned char)*text)) ++text;
    char *end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) --end;
    *end = '\0';
    return text;
}

static char *read_settings_file(const char *path, char *error, size_t error_capacity) {
    if (!path || !*path) {
        explain_failure(error, error_capacity, "A printer settings file path is required.");
        return NULL;
    }
    int descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (descriptor < 0) {
        explain_failure(error, error_capacity, "The printer settings file could not be opened: %s.", strerror(errno));
        return NULL;
    }
    struct stat original;
    if (fstat(descriptor, &original) < 0 || !S_ISREG(original.st_mode) || original.st_size < 0 ||
        (unsigned long long)original.st_size > MAXIMUM_SETTINGS_BYTES) {
        explain_failure(error, error_capacity, "The printer settings must be a regular file no larger than one mebibyte.");
        close(descriptor);
        return NULL;
    }
    size_t capacity = (size_t)original.st_size;
    char *contents = malloc(capacity + 1);
    if (!contents) {
        explain_failure(error, error_capacity, "There is not enough memory to read the printer settings.");
        close(descriptor);
        return NULL;
    }
    size_t used = 0;
    while (used < capacity) {
        ssize_t received = read(descriptor, contents + used, capacity - used);
        if (received < 0 && errno == EINTR) continue;
        if (received <= 0) {
            explain_failure(error, error_capacity, "The printer settings file could not be read completely.");
            free(contents);
            close(descriptor);
            return NULL;
        }
        used += (size_t)received;
    }
    char extra;
    ssize_t extra_count;
    do { extra_count = read(descriptor, &extra, 1); } while (extra_count < 0 && errno == EINTR);
    struct stat current;
    bool unchanged = fstat(descriptor, &current) == 0 && original.st_size == current.st_size &&
        original.st_mtim.tv_sec == current.st_mtim.tv_sec && original.st_mtim.tv_nsec == current.st_mtim.tv_nsec &&
        original.st_ctim.tv_sec == current.st_ctim.tv_sec && original.st_ctim.tv_nsec == current.st_ctim.tv_nsec;
    close(descriptor);
    if (extra_count != 0 || !unchanged || memchr(contents, '\0', used)) {
        explain_failure(error, error_capacity, "The printer settings changed while being read or contain invalid binary data.");
        free(contents);
        return NULL;
    }
    contents[used] = '\0';
    return contents;
}

static bool integer_in_range(const char *value, long minimum, long maximum, long *number) {
    if (!*value) return false;
    char *end;
    errno = 0;
    long parsed = strtol(value, &end, 10);
    if (errno || *end || end == value || parsed < minimum || parsed > maximum) return false;
    *number = parsed;
    return true;
}

static int selection_index(const char *value, const char *const *choices, size_t choice_count) {
    for (size_t choice_index = 0; choice_index < choice_count; ++choice_index)
        if (!strcmp(value, choices[choice_index])) return (int)choice_index;
    return -1;
}

static bool on_or_off(const char *value, bool *enabled) {
    if (!strcmp(value, "ON")) { *enabled = true; return true; }
    if (!strcmp(value, "OFF")) { *enabled = false; return true; }
    return false;
}

static int apply_setting(Bh7InputConfiguration *configuration, const char *key,
                          const char *value, unsigned *setting_index) {
    static const char *const keys[] = {"Media", "RResolution", "BiDirPrint", "MirrorPrint", "PaperType",
        "PaperThick", "PaperSource", "Copies", "ColorOrMono", "Collate", "Document", "ColorMatch",
        "HalfTone", "ColorEnhance", "SlowDrying", "Brightness", "Contrast", "RedKey", "GreenKey", "BlueKey", "Resolution"};
    size_t key_index;
    for (key_index = 0; key_index < sizeof(keys) / sizeof(keys[0]); ++key_index)
        if (!strcmp(key, keys[key_index])) break;
    if (key_index == sizeof(keys) / sizeof(keys[0])) return 1;
    *setting_index = (unsigned)key_index;
    Bh7PrintSettings *settings = &configuration->settings;
    int selected;
    long number;
    bool enabled;
    switch (key_index) {
    case 0: {
        static const char *const choices[] = {"Plain", "Inkjet", "BrotherGlossy", "Glossy", "Transparencies"};
        selected = selection_index(value, choices, sizeof(choices) / sizeof(choices[0]));
        if (selected < 0) return -1;
        settings->media = (Bh7Media)selected;
        return 0;
    }
    case 1: {
        static const char *const choices[] = {"Draft", "Fast", "Normal", "Fine", "Photo", "Highest"};
        selected = selection_index(value, choices, sizeof(choices) / sizeof(choices[0]));
        if (selected < 0) return -1;
        settings->quality = (Bh7Quality)selected;
        return 0;
    }
    case 2: return on_or_off(value, &settings->bidirectional) ? 0 : -1;
    case 3: return on_or_off(value, &settings->mirror) ? 0 : -1;
    case 4:
        if (strlen(value) >= sizeof(configuration->paper_name) || bh7_paper_code(value) < 0) return -1;
        strcpy(configuration->paper_name, value);
        return 0;
    case 5:
        if (!strcmp(value, "Regular")) settings->thick_paper = false;
        else if (!strcmp(value, "Thick")) settings->thick_paper = true;
        else return -1;
        return 0;
    case 6: {
        static const char *const choices[] = {"AUTO", "Tray1", "Tray2"};
        selected = selection_index(value, choices, sizeof(choices) / sizeof(choices[0]));
        if (selected < 0) return -1;
        settings->paper_source = (uint8_t)selected;
        return 0;
    }
    case 7: return integer_in_range(value, 1, 1, &number) ? 0 : -1;
    case 8:
        if (!strcmp(value, "Mono")) settings->monochrome = true;
        else if (!strcmp(value, "Color")) settings->monochrome = false;
        else return -1;
        return 0;
    case 9: return on_or_off(value, &enabled) ? 0 : -1;
    case 10:
        if (!strcmp(value, "Photo")) settings->document = BH7_DOCUMENT_PHOTO;
        else if (!strcmp(value, "Graphics")) settings->document = BH7_GRAPHICS;
        else if (!strcmp(value, "Custom")) settings->document = BH7_CUSTOM;
        else return -1;
        return 0;
    case 11:
        if (!strcmp(value, "Natural")) settings->color_match = BH7_NATURAL;
        else if (!strcmp(value, "Vivid")) settings->color_match = BH7_VIVID;
        else if (!strcmp(value, "None")) settings->color_match = BH7_NO_MATCH;
        else return -1;
        return 0;
    case 12:
        if (!strcmp(value, "Diffusion")) settings->halftone = BH7_DIFFUSION;
        else if (!strcmp(value, "Dither")) settings->halftone = BH7_DITHER;
        else return -1;
        return 0;
    case 13: return on_or_off(value, &settings->color_enhance) ? 0 : -1;
    case 14: return on_or_off(value, &settings->slow_drying) ? 0 : -1;
    case 15: case 16: case 17: case 18: case 19:
        if (!integer_in_range(value, -20, 20, &number)) return -1;
        switch (key_index) {
        case 15: settings->brightness = (int8_t)number; break;
        case 16: settings->contrast = (int8_t)number; break;
        case 17: settings->red = (int8_t)number; break;
        case 18: settings->green = (int8_t)number; break;
        default: settings->blue = (int8_t)number; break;
        }
        return 0;
    case 20:
        if (!integer_in_range(value, 150, 600, &number) || (number != 150 && number != 300 && number != 600)) return -1;
        configuration->raster_dpi = (uint16_t)number;
        return 0;
    default: return -1;
    }
}

int bh7_read_printer_configuration(const char *path,
                                    Bh7InputConfiguration *configuration,
                                    char *error, size_t error_capacity) {
    if (error && error_capacity) error[0] = '\0';
    if (!configuration) return explain_failure(error, error_capacity, "A printer configuration output object is required.");
    char *contents = read_settings_file(path, error, error_capacity);
    if (!contents) return -1;
    Bh7InputConfiguration parsed = {0};
    bh7_print_defaults(&parsed.settings);
    strcpy(parsed.paper_name, "A4");
    parsed.settings.paper_name = parsed.paper_name;
    bool printer_section = false, found_printer_section = false, any_section = false;
    bool seen[21] = {false};
    size_t line_number = 0;
    char *next_line = contents;
    while (next_line) {
        ++line_number;
        char *line = next_line;
        next_line = strchr(line, '\n');
        if (next_line) *next_line++ = '\0';
        if (line_number == 1 && strlen(line) >= 3 && (unsigned char)line[0] == 0xef &&
            (unsigned char)line[1] == 0xbb && (unsigned char)line[2] == 0xbf) line += 3;
        line = trim_whitespace(line);
        if (!*line || *line == '#' || *line == ';') continue;
        if (*line == '[') {
            char *end = strchr(line + 1, ']');
            if (!end) { explain_failure(error, error_capacity, "Configuration line %zu has an unterminated section.", line_number); goto failed; }
            char *tail = trim_whitespace(end + 1);
            if (*tail && *tail != '#' && *tail != ';') {
                explain_failure(error, error_capacity, "Configuration line %zu has unexpected text after a section.", line_number); goto failed;
            }
            *end = '\0';
            char *section_name = trim_whitespace(line + 1);
            if (!*section_name) { explain_failure(error, error_capacity, "Configuration line %zu has an empty section.", line_number); goto failed; }
            printer_section = !strcasecmp(section_name, "DCP150C");
            if (printer_section && found_printer_section) {
                explain_failure(error, error_capacity, "The printer configuration contains more than one DCP150C section."); goto failed;
            }
            found_printer_section = found_printer_section || printer_section;
            any_section = true;
            continue;
        }
        char *equal = strchr(line, '=');
        if (!equal || !any_section) {
            explain_failure(error, error_capacity, "Configuration line %zu must contain a setting inside a named section.", line_number); goto failed;
        }
        *equal = '\0';
        char *key = trim_whitespace(line), *value = trim_whitespace(equal + 1);
        if (!*key) { explain_failure(error, error_capacity, "Configuration line %zu has an empty setting name.", line_number); goto failed; }
        if (!printer_section) continue;
        unsigned setting_index = 0;
        int applied = apply_setting(&parsed, key, value, &setting_index);
        if (applied < 0) {
            explain_failure(error, error_capacity, "Configuration line %zu has an unsupported value for %s.", line_number, key); goto failed;
        }
        if (applied == 0) {
            if (seen[setting_index]) {
                explain_failure(error, error_capacity, "The printer configuration contains the setting %s more than once.", key); goto failed;
            }
            seen[setting_index] = true;
        }
    }
    if (!found_printer_section) {
        explain_failure(error, error_capacity, "The printer configuration does not contain a DCP150C section."); goto failed;
    }
    uint16_t expected_dpi = parsed.settings.quality == BH7_DRAFT ? 150 : parsed.settings.quality == BH7_FAST ? 300 : 600;
    if (parsed.raster_dpi && parsed.raster_dpi != expected_dpi) {
        explain_failure(error, error_capacity, "The configured raster resolution does not match the selected print quality."); goto failed;
    }
    parsed.raster_dpi = expected_dpi;
    *configuration = parsed;
    configuration->settings.paper_name = configuration->paper_name;
    free(contents);
    return 0;
failed:
    free(contents);
    return -1;
}

int bh7_read_paper_dimensions(const char *path, const char *paper_name,
                               uint32_t *width_600, uint32_t *height_600,
                               char *error, size_t error_capacity) {
    if (error && error_capacity) error[0] = '\0';
    if (!paper_name || !*paper_name || !width_600 || !height_600)
        return explain_failure(error, error_capacity, "A paper name and dimension output objects are required.");
    char *contents = read_settings_file(path, error, error_capacity);
    if (!contents) return -1;
    uint32_t selected_width = 0, selected_height = 0;
    char *next_line = contents;
    while (next_line) {
        char *line = next_line;
        next_line = strchr(line, '\n');
        if (next_line) *next_line++ = '\0';
        line = trim_whitespace(line);
        if (!*line || *line == '#' || *line == ';') continue;
        char *colon = strchr(line, ':');
        if (!colon) continue;
        *colon = '\0';
        if (strcmp(trim_whitespace(line), paper_name)) continue;
        if (selected_width) {
            explain_failure(error, error_capacity, "The paper dimensions file defines the selected paper more than once."); goto failed;
        }
        char *width_text = trim_whitespace(colon + 1), *height_text = width_text;
        while (*height_text && !isspace((unsigned char)*height_text)) ++height_text;
        if (!*height_text) { explain_failure(error, error_capacity, "The selected paper has incomplete dimensions."); goto failed; }
        *height_text++ = '\0';
        height_text = trim_whitespace(height_text);
        long width, height;
        if (!integer_in_range(width_text, 1, 16000, &width) || !integer_in_range(height_text, 1, 30000, &height)) {
            explain_failure(error, error_capacity, "The selected paper dimensions are invalid or exceed the printer raster limits."); goto failed;
        }
        selected_width = (uint32_t)width;
        selected_height = (uint32_t)height;
    }
    if (!selected_width) {
        explain_failure(error, error_capacity, "The paper dimensions file has no entry for the selected paper."); goto failed;
    }
    *width_600 = selected_width;
    *height_600 = selected_height;
    free(contents);
    return 0;
failed:
    free(contents);
    return -1;
}
