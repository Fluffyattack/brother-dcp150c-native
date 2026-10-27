/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Portable configuration tool for the Brother DCP-150C native driver. */
#define _POSIX_C_SOURCE 200809L

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define DEFAULT_CONFIGURATION_PATH "/etc/dcp150c-native/default.conf"
#define MAXIMUM_CONFIGURATION_BYTES (1024U * 1024U)
#define MAXIMUM_OUTPUT_BYTES (MAXIMUM_CONFIGURATION_BYTES + 16384U)
#define SETTING_VALUE_CAPACITY 64

struct SettingDefinition {
    const char *key;
    const char *classic;
    const char *option;
    const char *choices;
    const char *initial;
    int minimum;
    int maximum;
};

static const struct SettingDefinition setting_definitions[] = {
    {"Media", "-md", "--media", "Plain,Inkjet,BrotherGlossy,Glossy,Transparencies", "Plain", 0, 0},
    {"RResolution", "-reso", "--resolution", "Draft,Fast,Normal,Fine,Photo,Highest", "Normal", 0, 0},
    {"BiDirPrint", "-bidir", "--bidir", "OFF,ON", "ON", 0, 0},
    {"MirrorPrint", "-mirro", "--mirror", "OFF,ON", "OFF", 0, 0},
    {"PaperType", "-pt", "--paper-size",
     "A4,BrA4_B,Letter,BrLetter_B,Legal,Executive,B5,A5,A6,BrA6_B,PostC4x6,BrPostC4x6_B,"
     "IndexC5x8,BrIndexC5x8_B,PhotoL,BrPhotoL_B,Photo2L,BrPhoto2L_B,PostCard,BrHagaki_B,"
     "DoublePostCardRotated,EnvC5,EnvDL,Env10,EnvMonarch,EnvYou4", "A4", 0, 0},
    {"PaperThick", "-thick", "--paper-thickness", "Regular,Thick", "Regular", 0, 0},
    {"PaperSource", NULL, "--paper-source", "AUTO,Tray1,Tray2", "AUTO", 0, 0},
    {"Copies", "-copy", "--copies", NULL, "1", 1, 1},
    {"ColorOrMono", "-corm", "--color", "Mono,Color", "Color", 0, 0},
    {"Collate", "-collate", "--collate", "OFF,ON", "OFF", 0, 0},
    {"Document", "-doc", "--document", "Photo,Graphics,Custom", "Photo", 0, 0},
    {"ColorMatch", "-cm", "--color-match", "Natural,Vivid,None", "Natural", 0, 0},
    {"HalfTone", "-ht", "--halftone", "Diffusion,Dither", "Diffusion", 0, 0},
    {"ColorEnhance", "-ce", "--color-enhance", "OFF,ON", "OFF", 0, 0},
    {"SlowDrying", "-sd", "--slow-drying", "OFF,ON", "OFF", 0, 0},
    {"Brightness", "-brit", "--brightness", NULL, "0", -20, 20},
    {"Contrast", "-cont", "--contrast", NULL, "0", -20, 20},
    {"RedKey", "-red", "--red", NULL, "0", -20, 20},
    {"GreenKey", "-green", "--green", NULL, "0", -20, 20},
    {"BlueKey", "-blue", "--blue", NULL, "0", -20, 20},
};

enum { SUPPORTED_SETTING_COUNT = sizeof(setting_definitions) / sizeof(setting_definitions[0]), DERIVED_RESOLUTION_INDEX = SUPPORTED_SETTING_COUNT };

struct ConfigurationLine {
    size_t offset;
    size_t length;
    int field;
};

struct ConfigurationDocument {
    char *text;
    size_t length;
    struct ConfigurationLine *lines;
    size_t line_count;
    size_t insert_at;
    bool has_section;
    bool seen[SUPPORTED_SETTING_COUNT + 1];
    char values[SUPPORTED_SETTING_COUNT][SETTING_VALUE_CAPACITY];
    char resolution[SETTING_VALUE_CAPACITY];
};

struct ConfigurationOutput {
    char *data;
    size_t length;
    size_t capacity;
};

static void report_configuration_error(const char *format, ...) {
    va_list args;
    fputs("Printer configuration: ", stderr);
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    fputc('\n', stderr);
}

static char *trim_whitespace(char *text) {
    while (*text && isspace((unsigned char)*text)) ++text;
    char *end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) --end;
    *end = '\0';
    return text;
}

static bool is_valid_setting_name(const char *key) {
    if (!isalpha((unsigned char)*key) && *key != '_') return false;
    for (++key; *key; ++key) {
        if (!isalnum((unsigned char)*key) && *key != '_' && *key != '.' && *key != '-') return false;
    }
    return true;
}

static int find_setting_by_name(const char *key) {
    for (int entry_index = 0; entry_index < SUPPORTED_SETTING_COUNT; ++entry_index) {
        if (!strcmp(key, setting_definitions[entry_index].key)) return entry_index;
    }
    return !strcmp(key, "Resolution") ? DERIVED_RESOLUTION_INDEX : -1;
}

static int find_setting_by_option(const char *option) {
    for (int entry_index = 0; entry_index < SUPPORTED_SETTING_COUNT; ++entry_index) {
        if (!strcmp(option, setting_definitions[entry_index].option) ||
            (setting_definitions[entry_index].classic && !strcmp(option, setting_definitions[entry_index].classic))) return entry_index;
    }
    return -1;
}

static bool is_supported_selection(const char *choices, const char *value) {
    size_t length = strlen(value);
    for (const char *cursor = choices; *cursor;) {
        const char *end = strchr(cursor, ',');
        size_t item_length = end ? (size_t)(end - cursor) : strlen(cursor);
        if (length == item_length && !strncmp(cursor, value, length)) return true;
        if (!end) break;
        cursor = end + 1;
    }
    return false;
}

static bool validate_integer_setting(const char *value, int minimum, int maximum, char result[SETTING_VALUE_CAPACITY]) {
    if (!*value) return false;
    char *end;
    errno = 0;
    long number = strtol(value, &end, 10);
    if (errno || *end || end == value || number < minimum || number > maximum) return false;
    snprintf(result, SETTING_VALUE_CAPACITY, "%ld", number);
    return true;
}

static bool validate_setting_value(int field, const char *value, char result[SETTING_VALUE_CAPACITY]) {
    if (field == DERIVED_RESOLUTION_INDEX) {
        if (!validate_integer_setting(value, 150, 600, result)) return false;
        return !strcmp(result, "150") || !strcmp(result, "300") || !strcmp(result, "600");
    }
    if (setting_definitions[field].choices) {
        if (!is_supported_selection(setting_definitions[field].choices, value) || strlen(value) >= SETTING_VALUE_CAPACITY) return false;
        strcpy(result, value);
        return true;
    }
    return validate_integer_setting(value, setting_definitions[field].minimum, setting_definitions[field].maximum, result);
}

static const char *derive_raster_resolution(const char *quality) {
    if (!strcmp(quality, "Draft")) return "150";
    if (!strcmp(quality, "Fast")) return "300";
    return "600";
}

static void initialize_configuration_document(struct ConfigurationDocument *configuration) {
    memset(configuration, 0, sizeof(*configuration));
    for (int entry_index = 0; entry_index < SUPPORTED_SETTING_COUNT; ++entry_index) strcpy(configuration->values[entry_index], setting_definitions[entry_index].initial);
    strcpy(configuration->resolution, "600");
}

static void release_configuration_document(struct ConfigurationDocument *configuration) {
    free(configuration->text);
    free(configuration->lines);
}

static bool has_same_file_metadata(const struct stat *original_metadata, const struct stat *current_metadata) {
    return original_metadata->st_dev == current_metadata->st_dev && original_metadata->st_ino == current_metadata->st_ino && original_metadata->st_size == current_metadata->st_size &&
           original_metadata->st_mode == current_metadata->st_mode && original_metadata->st_uid == current_metadata->st_uid && original_metadata->st_gid == current_metadata->st_gid &&
           original_metadata->st_mtim.tv_sec == current_metadata->st_mtim.tv_sec && original_metadata->st_mtim.tv_nsec == current_metadata->st_mtim.tv_nsec &&
           original_metadata->st_ctim.tv_sec == current_metadata->st_ctim.tv_sec && original_metadata->st_ctim.tv_nsec == current_metadata->st_ctim.tv_nsec;
}

static int read_configuration_document(int directory_descriptor, const char *filename, struct ConfigurationDocument *configuration,
                         struct stat *original_metadata, bool *configuration_exists) {
    int file_descriptor = openat(directory_descriptor, filename, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (file_descriptor < 0) {
        if (errno == ENOENT) { *configuration_exists = false; return 0; }
        report_configuration_error("The printer configuration could not be opened: %s.", strerror(errno));
        return -1;
    }
    *configuration_exists = true;
    if (fstat(file_descriptor, original_metadata) < 0) {
        report_configuration_error("The printer configuration could not be inspected: %s.", strerror(errno));
        close(file_descriptor);
        return -1;
    }
    if (!S_ISREG(original_metadata->st_mode) || original_metadata->st_size < 0 || (unsigned long long)original_metadata->st_size > MAXIMUM_CONFIGURATION_BYTES) {
        report_configuration_error("The printer configuration must be a regular file no larger than %u bytes.", MAXIMUM_CONFIGURATION_BYTES);
        close(file_descriptor);
        return -1;
    }
    configuration->text = malloc(MAXIMUM_CONFIGURATION_BYTES + 1U);
    if (!configuration->text) { report_configuration_error("There is not enough memory to process the printer configuration."); close(file_descriptor); return -1; }
    while (configuration->length < MAXIMUM_CONFIGURATION_BYTES) {
        ssize_t byte_count = read(file_descriptor, configuration->text + configuration->length, MAXIMUM_CONFIGURATION_BYTES - configuration->length);
        if (byte_count < 0 && errno == EINTR) continue;
        if (byte_count < 0) { report_configuration_error("The printer configuration could not be read: %s.", strerror(errno)); close(file_descriptor); return -1; }
        if (!byte_count) break;
        configuration->length += (size_t)byte_count;
    }
    char extra;
    ssize_t extra_count;
    do { extra_count = read(file_descriptor, &extra, 1); } while (extra_count < 0 && errno == EINTR);
    struct stat metadata_after_read;
    if (extra_count != 0 || fstat(file_descriptor, &metadata_after_read) < 0 || !has_same_file_metadata(original_metadata, &metadata_after_read)) {
        report_configuration_error("The printer configuration changed while being read or exceeds the size limit.");
        close(file_descriptor);
        return -1;
    }
    close(file_descriptor);
    if (memchr(configuration->text, '\0', configuration->length)) { report_configuration_error("The printer configuration contains invalid binary data."); return -1; }
    configuration->text[configuration->length] = '\0';
    return 0;
}

static int parse_configuration_document(struct ConfigurationDocument *configuration) {
    bool target = false, any_section = false;
    size_t offset = 0;
    size_t line_capacity = 1;
    for (size_t entry_index = 0; entry_index < configuration->length; ++entry_index) if (configuration->text[entry_index] == '\n') ++line_capacity;
    configuration->lines = calloc(line_capacity, sizeof(*configuration->lines));
    if (!configuration->lines) { report_configuration_error("There is not enough memory to process the printer configuration."); return -1; }
    while (offset < configuration->length) {
        const char *newline = memchr(configuration->text + offset, '\n', configuration->length - offset);
        size_t length = newline ? (size_t)(newline - configuration->text) + 1 - offset : configuration->length - offset;
        size_t index = configuration->line_count++;
        configuration->lines[index] = (struct ConfigurationLine){offset, length, -1};
        char *copy = strndup(configuration->text + offset, length);
        if (!copy) { report_configuration_error("There is not enough memory to process the printer configuration."); return -1; }
        char *cursor = copy;
        if (!offset && length >= 3 && (unsigned char)cursor[0] == 0xef &&
            (unsigned char)cursor[1] == 0xbb && (unsigned char)cursor[2] == 0xbf) cursor += 3;
        cursor = trim_whitespace(cursor);
        if (!*cursor || *cursor == '#' || *cursor == ';') {
            free(copy);
            offset += length;
            continue;
        }
        if (*cursor == '[') {
            char *end = strchr(cursor + 1, ']');
            if (!end) { report_configuration_error("Configuration line %zu has an unterminated section.", index + 1); free(copy); return -1; }
            char *tail = trim_whitespace(end + 1);
            if (*tail && *tail != '#' && *tail != ';') {
                report_configuration_error("Configuration line %zu has unexpected text after a section.", index + 1); free(copy); return -1;
            }
            *end = '\0';
            char *section = trim_whitespace(cursor + 1);
            if (!*section) { report_configuration_error("Configuration line %zu has an empty section name.", index + 1); free(copy); return -1; }
            if (target) configuration->insert_at = index;
            target = !strcasecmp(section, "DCP150C");
            if (target && configuration->has_section) {
                report_configuration_error("Configuration line %zu repeats the DCP150C section.", index + 1); free(copy); return -1;
            }
            if (target) configuration->has_section = true;
            any_section = true;
        } else {
            char *equal = strchr(cursor, '=');
            if (!equal || !any_section) {
                report_configuration_error("Configuration line %zu must contain a setting inside a named section.", index + 1); free(copy); return -1;
            }
            *equal = '\0';
            char *key = trim_whitespace(cursor), *value = trim_whitespace(equal + 1);
            if (!is_valid_setting_name(key)) { report_configuration_error("Configuration line %zu has an invalid setting name.", index + 1); free(copy); return -1; }
            int field = target ? find_setting_by_name(key) : -1;
            if (field >= 0) {
                char validated[SETTING_VALUE_CAPACITY];
                if (configuration->seen[field] || !validate_setting_value(field, value, validated)) {
                    report_configuration_error("Configuration line %zu repeats %s or gives it an unsupported value.", index + 1, key);
                    free(copy);
                    return -1;
                }
                configuration->seen[field] = true;
                configuration->lines[index].field = field;
                strcpy(field == DERIVED_RESOLUTION_INDEX ? configuration->resolution : configuration->values[field], validated);
            }
        }
        free(copy);
        offset += length;
    }
    if (target) configuration->insert_at = configuration->line_count;
    if (any_section && !configuration->has_section) {
        report_configuration_error("The printer configuration does not contain a DCP150C section.");
        return -1;
    }
    return 0;
}

static int append_output_bytes(struct ConfigurationOutput *rendered_output, const void *data, size_t length) {
    if (length > MAXIMUM_OUTPUT_BYTES - rendered_output->length) { report_configuration_error("The updated printer configuration exceeds the supported size limit."); return -1; }
    size_t needed = rendered_output->length + length;
    if (needed > rendered_output->capacity) {
        size_t capacity = rendered_output->capacity ? rendered_output->capacity : 1024;
        while (capacity < needed) capacity *= 2;
        char *resized = realloc(rendered_output->data, capacity);
        if (!resized) { report_configuration_error("There is not enough memory to process the printer configuration."); return -1; }
        rendered_output->data = resized;
        rendered_output->capacity = capacity;
    }
    memcpy(rendered_output->data + rendered_output->length, data, length);
    rendered_output->length += length;
    return 0;
}

static int append_output_setting(struct ConfigurationOutput *rendered_output, const char *key, const char *value) {
    char line[128];
    int byte_count = snprintf(line, sizeof(line), "%s=%s\n", key, value);
    if (byte_count < 0 || (size_t)byte_count >= sizeof(line)) { report_configuration_error("The printer setting is longer than the supported size limit."); return -1; }
    return append_output_bytes(rendered_output, line, (size_t)byte_count);
}

static int append_missing_settings(struct ConfigurationOutput *rendered_output, const struct ConfigurationDocument *configuration) {
    bool missing = !configuration->seen[DERIVED_RESOLUTION_INDEX];
    for (int entry_index = 0; entry_index < SUPPORTED_SETTING_COUNT; ++entry_index) missing = missing || !configuration->seen[entry_index];
    if (missing && rendered_output->length && rendered_output->data[rendered_output->length - 1] != '\n' && append_output_bytes(rendered_output, "\n", 1) < 0) return -1;
    for (int entry_index = 0; entry_index < SUPPORTED_SETTING_COUNT; ++entry_index) {
        if (!configuration->seen[entry_index] && append_output_setting(rendered_output, setting_definitions[entry_index].key, configuration->values[entry_index]) < 0) return -1;
    }
    if (!configuration->seen[DERIVED_RESOLUTION_INDEX] && append_output_setting(rendered_output, "Resolution", configuration->resolution) < 0) return -1;
    return 0;
}

static int render_updated_configuration(struct ConfigurationOutput *rendered_output, const struct ConfigurationDocument *configuration, const bool updated_settings[SUPPORTED_SETTING_COUNT]) {
    for (size_t entry_index = 0; entry_index <= configuration->line_count; ++entry_index) {
        if (configuration->has_section && entry_index == configuration->insert_at && append_missing_settings(rendered_output, configuration) < 0) return -1;
        if (entry_index == configuration->line_count) break;
        const struct ConfigurationLine *line = &configuration->lines[entry_index];
        int field = line->field;
        if (field == DERIVED_RESOLUTION_INDEX || (field >= 0 && updated_settings[field])) {
            const char *key = field == DERIVED_RESOLUTION_INDEX ? "Resolution" : setting_definitions[field].key;
            const char *value = field == DERIVED_RESOLUTION_INDEX ? configuration->resolution : configuration->values[field];
            if (append_output_setting(rendered_output, key, value) < 0) return -1;
        } else if (append_output_bytes(rendered_output, configuration->text + line->offset, line->length) < 0) return -1;
    }
    if (!configuration->has_section) {
        if (rendered_output->length && rendered_output->data[rendered_output->length - 1] != '\n' && append_output_bytes(rendered_output, "\n", 1) < 0) return -1;
        if (append_output_bytes(rendered_output, "[DCP150C]\n", 10) < 0 || append_missing_settings(rendered_output, configuration) < 0 ||
            append_output_setting(rendered_output, "Language", "us") < 0) return -1;
    }
    return 0;
}

static int lock_configuration_directory(int file_descriptor) {
    struct timespec lock_start_time, current_time, retry_delay = {0, 50000000};
    if (clock_gettime(CLOCK_MONOTONIC, &lock_start_time) < 0) return -1;
    for (;;) {
        if (flock(file_descriptor, LOCK_EX | LOCK_NB) == 0) return 0;
        if (errno == EINTR) continue;
        if (errno != EWOULDBLOCK && errno != EAGAIN) return -1;
        if (clock_gettime(CLOCK_MONOTONIC, &current_time) < 0) return -1;
        if (current_time.tv_sec - lock_start_time.tv_sec >= 5) { errno = EWOULDBLOCK; return -1; }
        nanosleep(&retry_delay, NULL);
    }
}

static int synchronize_file_contents(int file_descriptor) {
    int result;
    do { result = fsync(file_descriptor); } while (result < 0 && errno == EINTR);
    return result;
}

static int replace_configuration_atomically(int directory_descriptor, const char *filename, const struct ConfigurationOutput *rendered_output,
                          bool configuration_exists, const struct stat *original_metadata) {
    char temporary_filename[80];
    int file_descriptor = -1;
    for (unsigned attempt = 0; attempt < 100; ++attempt) {
        snprintf(temporary_filename, sizeof(temporary_filename), ".dcp150c-config.%ld.%u", (long)getpid(), attempt);
        file_descriptor = openat(directory_descriptor, temporary_filename, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (file_descriptor >= 0 || errno != EEXIST) break;
    }
    if (file_descriptor < 0) { report_configuration_error("A temporary printer configuration could not be created: %s.", strerror(errno)); return -1; }
    int result = -1;
    size_t written = 0;
    while (written < rendered_output->length) {
        ssize_t byte_count = write(file_descriptor, rendered_output->data + written, rendered_output->length - written);
        if (byte_count < 0 && errno == EINTR) continue;
        if (byte_count <= 0) { report_configuration_error("The temporary printer configuration could not be written: %s.", strerror(errno)); goto finish; }
        written += (size_t)byte_count;
    }
    mode_t permission_mode;
    if (configuration_exists) {
        if (fchown(file_descriptor, original_metadata->st_uid, original_metadata->st_gid) < 0) {
            report_configuration_error("The printer configuration owner and group could not be preserved: %s.", strerror(errno)); goto finish;
        }
        permission_mode = original_metadata->st_mode & 0777;
    } else {
        mode_t creation_mask = umask(0);
        umask(creation_mask);
        permission_mode = 0644 & ~creation_mask;
    }
    if (fchmod(file_descriptor, permission_mode) < 0 || synchronize_file_contents(file_descriptor) < 0) {
        report_configuration_error("The temporary printer configuration could not be finalized: %s.", strerror(errno)); goto finish;
    }
    if (close(file_descriptor) < 0) { file_descriptor = -1; report_configuration_error("The temporary printer configuration could not be closed: %s.", strerror(errno)); goto finish; }
    file_descriptor = -1;
    struct stat current_metadata;
    int current_file_status = fstatat(directory_descriptor, filename, &current_metadata, AT_SYMLINK_NOFOLLOW);
    if ((configuration_exists && (current_file_status < 0 || !has_same_file_metadata(original_metadata, &current_metadata))) ||
        (!configuration_exists && (current_file_status == 0 || errno != ENOENT))) {
        report_configuration_error("The printer configuration changed before replacement. Retry the command."); goto finish;
    }
    if (renameat(directory_descriptor, temporary_filename, directory_descriptor, filename) < 0) {
        report_configuration_error("The printer configuration could not be replaced: %s.", strerror(errno)); goto finish;
    }
    result = 0;
    if (synchronize_file_contents(directory_descriptor) < 0) {
        report_configuration_error("The printer configuration was saved, but directory synchronization failed: %s.", strerror(errno));
        result = -1;
    }
finish:
    if (file_descriptor >= 0) close(file_descriptor);
    if (result < 0) unlinkat(directory_descriptor, temporary_filename, 0);
    return result;
}

static void print_supported_settings(const char *program) {
    printf("Usage on DCP150C:\n  %s [-P dcp150c] [-rc FILE] [OPTION VALUE]...\n", program);
    puts("  --get              print effective settings without changing the file\n"
         "  --capabilities     list supported settings without opening a configuration\n"
         "  -h, --help         show this help\n"
         "  -rc, --config FILE override DCP150C_CONFIG or " DEFAULT_CONFIGURATION_PATH "\n\n"
         "Settings (values are case-sensitive):");
    for (int entry_index = 0; entry_index < SUPPORTED_SETTING_COUNT; ++entry_index) {
        printf("  %-9s %-19s %s\n      ", setting_definitions[entry_index].classic ? setting_definitions[entry_index].classic : "", setting_definitions[entry_index].option, setting_definitions[entry_index].key);
        if (setting_definitions[entry_index].choices) puts(setting_definitions[entry_index].choices);
        else printf("integer %d..%d\n", setting_definitions[entry_index].minimum, setting_definitions[entry_index].maximum);
    }
    puts("\nResolution is derived from RResolution: Draft=150, Fast=300, other modes=600.\n"
         "Copies is fixed at 1; request multiple copies through CUPS.\n"
         "Updates preserve unknown keys, comments, and unrelated sections.\n"
         "Existing malformed or ambiguous settings must be corrected before an update.");
}

static void print_effective_settings(const struct ConfigurationDocument *configuration, bool configuration_exists) {
    if (!configuration_exists) puts("# Defaults; configuration file does not exist.");
    puts("[DCP150C]");
    for (int entry_index = 0; entry_index < SUPPORTED_SETTING_COUNT; ++entry_index) printf("%s=%s\n", setting_definitions[entry_index].key, configuration->values[entry_index]);
    printf("Resolution=%s\n", configuration->seen[DERIVED_RESOLUTION_INDEX] ? configuration->resolution : derive_raster_resolution(configuration->values[1]));
}

int main(int argument_count, char **arguments) {
    const char *configuration_path = getenv("DCP150C_CONFIG");
    if (!configuration_path || !*configuration_path) configuration_path = DEFAULT_CONFIGURATION_PATH;
    char requested_values[SUPPORTED_SETTING_COUNT][SETTING_VALUE_CAPACITY];
    bool updated_settings[SUPPORTED_SETTING_COUNT] = {false};
    bool updating = false, help_requested = false, capabilities_requested = false, query_requested = false;
    for (int entry_index = 1; entry_index < argument_count; ++entry_index) {
        const char *option = arguments[entry_index];
        if (!strcmp(option, "--help") || !strcmp(option, "-h")) { help_requested = true; continue; }
        if (!strcmp(option, "--capabilities")) { capabilities_requested = true; continue; }
        if (!strcmp(option, "--get")) { query_requested = true; continue; }
        if (!strcmp(option, "-P")) {
            if (++entry_index == argument_count || (strcasecmp(arguments[entry_index], "dcp150c") && strcasecmp(arguments[entry_index], "dcp-150c"))) {
                report_configuration_error("The -P option requires the printer name dcp150c."); return 2;
            }
            continue;
        }
        if (!strcmp(option, "-rc") || !strcmp(option, "--config")) {
            if (++entry_index == argument_count || !*arguments[entry_index]) { report_configuration_error("The option %s requires a configuration file path.", option); return 2; }
            configuration_path = arguments[entry_index];
            continue;
        }
        int field = find_setting_by_option(option);
        if (field < 0) { report_configuration_error("The option %s is not supported. Use --help to see the available options.", option); return 2; }
        if (++entry_index == argument_count || !validate_setting_value(field, arguments[entry_index], requested_values[field])) {
            report_configuration_error("The option %s requires a supported value. Use --capabilities to see the allowed values.", option); return 2;
        }
        updated_settings[field] = true;
        updating = true;
    }
    if (updating && (help_requested || capabilities_requested || query_requested)) {
        report_configuration_error("Query and help options cannot be combined with configuration updates."); return 2;
    }
    if (help_requested || capabilities_requested) { print_supported_settings(arguments[0]); return ferror(stdout) ? 1 : 0; }

    char *path_copy = strdup(configuration_path);
    if (!path_copy) { report_configuration_error("There is not enough memory to process the printer configuration."); return 1; }
    char *slash = strrchr(path_copy, '/');
    const char *directory_path = ".", *filename = path_copy;
    if (slash) {
        *slash = '\0';
        filename = slash + 1;
        directory_path = slash == path_copy ? "/" : path_copy;
    }
    if (!*filename || !strcmp(filename, ".") || !strcmp(filename, "..")) {
        report_configuration_error("The configuration path must identify a file."); free(path_copy); return 2;
    }
    struct ConfigurationDocument configuration;
    initialize_configuration_document(&configuration);
    struct stat original_metadata = {0};
    bool configuration_exists = false;
    int directory_descriptor = open(directory_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory_descriptor < 0 && (!updating && errno == ENOENT)) {
        if (!query_requested) { printf("Configuration: %s\n\n", configuration_path); print_supported_settings(arguments[0]); puts("\nEffective settings:"); }
        print_effective_settings(&configuration, false);
        free(path_copy);
        return ferror(stdout) ? 1 : 0;
    }
    if (directory_descriptor < 0) {
        report_configuration_error("The printer configuration directory could not be opened: %s.", strerror(errno)); free(path_copy); return 1;
    }
    int result = 1;
    if (updating && lock_configuration_directory(directory_descriptor) < 0) {
        report_configuration_error("The printer configuration directory could not be locked: %s.", strerror(errno)); goto finish;
    }
    if (read_configuration_document(directory_descriptor, filename, &configuration, &original_metadata, &configuration_exists) < 0 || parse_configuration_document(&configuration) < 0) goto finish;
    if (!updating) {
        if (!query_requested) { printf("Configuration: %s\n\n", configuration_path); print_supported_settings(arguments[0]); puts("\nEffective settings:"); }
        print_effective_settings(&configuration, configuration_exists);
        result = ferror(stdout) ? 1 : 0;
        goto finish;
    }
    for (int entry_index = 0; entry_index < SUPPORTED_SETTING_COUNT; ++entry_index) if (updated_settings[entry_index]) strcpy(configuration.values[entry_index], requested_values[entry_index]);
    strcpy(configuration.resolution, derive_raster_resolution(configuration.values[1]));
    struct ConfigurationOutput rendered_output = {0};
    if (render_updated_configuration(&rendered_output, &configuration, updated_settings) == 0) result = replace_configuration_atomically(directory_descriptor, filename, &rendered_output, configuration_exists, &original_metadata) == 0 ? 0 : 1;
    free(rendered_output.data);
finish:
    close(directory_descriptor);
    release_configuration_document(&configuration);
    free(path_copy);
    return result;
}
