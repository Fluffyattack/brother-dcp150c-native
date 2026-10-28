// SPDX-License-Identifier: GPL-3.0-or-later
#include "device_access.h"

#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iomanip>
#include <locale>
#include <sstream>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <vector>

namespace network_scanner {
namespace {

std::string environment(const char* name, const char* fallback = "") {
  const char* value = ::getenv(name);
  return value ? value : fallback;
}

bool splitAbsolutePath(const std::string& path, std::vector<std::string>& parts) {
  if (path.size() < 2 || path.size() > 4096 || path[0] != '/' || path.back() == '/')
    return false;
  std::istringstream stream(path.substr(1));
  std::string component;
  while (std::getline(stream, component, '/')) {
    if (component.empty() || component == "." || component == "..")
      return false;
    parts.push_back(component);
  }
  return !parts.empty();
}

int openWithoutSymlinks(const std::string& path, int flags) {
  std::vector<std::string> parts;
  if (!splitAbsolutePath(path, parts)) {
    errno = EINVAL;
    return -1;
  }
  int directory = ::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (directory < 0)
    return -1;
  for (size_t index = 0; index + 1 < parts.size(); ++index) {
    int child = ::openat(directory, parts[index].c_str(),
                         O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    int saved_error = errno;
    ::close(directory);
    if (child < 0) {
      errno = saved_error;
      return -1;
    }
    directory = child;
  }
  int result = ::openat(directory, parts.back().c_str(),
                        flags | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
  int saved_error = errno;
  ::close(directory);
  errno = saved_error;
  return result;
}

bool sameRegularFile(int descriptor, const std::string& path) {
  struct stat opened = {}, current = {};
  int second = openWithoutSymlinks(path, O_PATH);
  if (second < 0)
    return false;
  bool valid = ::fstat(descriptor, &opened) == 0 && ::fstat(second, &current) == 0
    && S_ISREG(opened.st_mode) && S_ISREG(current.st_mode) && opened.st_nlink == 1
    && opened.st_dev == current.st_dev && opened.st_ino == current.st_ino;
  ::close(second);
  return valid;
}

bool writeAll(int descriptor, const std::string& content) {
  size_t position = 0;
  while (position < content.size()) {
    ssize_t count = ::write(descriptor, content.data() + position, content.size() - position);
    if (count < 0 && errno == EINTR)
      continue;
    if (count <= 0)
      return false;
    position += static_cast<size_t>(count);
  }
  return true;
}

bool testBackendRequested() {
  return environment("DCP150C_NETWORK_SCANNER_ALLOW_TEST_BACKEND") == "1";
}

bool isSystemPath(const std::string& path) {
  for (const char* root : {"/dev", "/sys", "/proc", "/etc", "/run", "/usr", "/var"}) {
    std::string prefix(root);
    if (path == prefix || path.compare(0, prefix.size() + 1, prefix + "/") == 0)
      return true;
  }
  return false;
}

bool isolatedTestConfiguration(const std::string& lock_path, const std::string& state_path) {
  std::string configuration = environment("SANE_CONFIG_DIR");
  if (isSystemPath(configuration) || isSystemPath(lock_path) || isSystemPath(state_path)
      || configuration.find(':') != std::string::npos)
    return false;
  int directory = openWithoutSymlinks(configuration, O_RDONLY | O_DIRECTORY);
  if (directory < 0)
    return false;
  struct stat information = {};
  bool valid = ::fstat(directory, &information) == 0 && information.st_uid == ::geteuid()
    && (information.st_mode & 0022) == 0;
  // An absent dll.d prevents additional backends from joining this fixture.
  struct stat extra_backends = {};
  if (::fstatat(directory, "dll.d", &extra_backends, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT)
    valid = false;
  int configuration_file = ::openat(directory, "dll.conf", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  ::close(directory);
  if (configuration_file < 0)
    return false;
  char content[1025] = {};
  ssize_t count = ::read(configuration_file, content, sizeof(content));
  valid = valid && ::fstat(configuration_file, &information) == 0 && S_ISREG(information.st_mode)
    && information.st_uid == ::geteuid() && (information.st_mode & 0022) == 0
    && count > 0 && count < 1025;
  ::close(configuration_file);
  if (!valid)
    return false;
  std::istringstream lines(std::string(content, static_cast<size_t>(count)));
  std::string line;
  int backend_count = 0;
  while (std::getline(lines, line)) {
    line = line.substr(0, line.find('#'));
    std::istringstream tokens(line);
    std::string backend, unexpected;
    if (!(tokens >> backend))
      continue;
    if (backend != "test" || tokens >> unexpected)
      return false;
    ++backend_count;
  }
  return backend_count == 1;
}

bool decimalRange(const std::string& value, size_t begin, size_t end) {
  if (begin == end || end - begin > 8 || value[begin] == '0')
    return false;
  for (size_t index = begin; index < end; ++index)
    if (value[index] < '0' || value[index] > '9')
      return false;
  return true;
}

} // namespace

void reportDeviceAccessFailure(const char* phase, AccessFailure failure) noexcept {
  const char* status = failure == AccessFailure::busy ? "busy" : "denied";
  std::fprintf(stderr, "network-scanner: device_access phase=%s status=%s\n", phase, status);
}

DeviceAccessLease::DeviceAccessLease(int lock, int state, AccessKind kind, double acquired)
  : lock_descriptor(lock), state_descriptor(state), access_kind(kind), acquired_monotonic(acquired) {}

bool DeviceAccessLease::publishState() noexcept {
  static std::atomic<unsigned long> sequence(0);
  std::string temporary_name;
  int output = -1;
  try {
    temporary_name = ".lease-" + std::to_string(::getpid()) + "-" + std::to_string(++sequence) + ".tmp";
    struct stat previous = {};
    if (::fstatat(state_descriptor, "lease.json", &previous, AT_SYMLINK_NOFOLLOW) == 0) {
      if (!S_ISREG(previous.st_mode) || previous.st_uid != ::geteuid() || previous.st_nlink != 1)
        return false;
    } else if (errno != ENOENT) {
      return false;
    }
    output = ::openat(state_descriptor, temporary_name.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (output < 0)
      return false;
    std::ostringstream content;
    content.imbue(std::locale::classic());
    content << "{\"process_id\":" << ::getpid() << ",\"acquired_monotonic\":"
            << std::setprecision(17) << acquired_monotonic << ",\"kind\":\""
            << (access_kind == AccessKind::session ? "session" : "metadata") << "\"}\n";
    bool written = ::fchmod(output, 0600) == 0 && writeAll(output, content.str()) && ::fsync(output) == 0;
    struct stat information = {};
    written = written && ::fstat(output, &information) == 0;
    ::close(output);
    output = -1;
    if (!written || ::renameat(state_descriptor, temporary_name.c_str(), state_descriptor, "lease.json") != 0) {
      ::unlinkat(state_descriptor, temporary_name.c_str(), 0);
      return false;
    }
    marker_device = information.st_dev;
    marker_inode = information.st_ino;
    published = true;
    return true;
  } catch (...) {
    if (output >= 0)
      ::close(output);
    if (!temporary_name.empty())
      ::unlinkat(state_descriptor, temporary_name.c_str(), 0);
    return false;
  }
}

DeviceAccessLease::~DeviceAccessLease() {
  if (published) {
    struct stat information = {};
    if (::fstatat(state_descriptor, "lease.json", &information, AT_SYMLINK_NOFOLLOW) != 0
        || information.st_dev != marker_device || information.st_ino != marker_inode
        || ::unlinkat(state_descriptor, "lease.json", 0) != 0)
      reportDeviceAccessFailure("lease_marker_cleanup_failed", AccessFailure::denied);
  }
  // Removal is atomic and precedes unlock. All backend cleanup has already
  // completed when the captured shared_ptr releases this final lease owner.
  ::close(state_descriptor);
  ::close(lock_descriptor);
}

AccessResult acquireDeviceAccess(AccessKind kind) noexcept {
  AccessResult result;
  int lock = -1, state = -1;
  try {
    std::string lock_path = environment("DCP150C_LOCK_PATH", "/run/lock/dcp150c-device.lock");
    std::string state_path = environment("DCP150C_NETWORK_SCANNER_STATE_DIRECTORY");
    if (testBackendRequested() && !isolatedTestConfiguration(lock_path, state_path))
      return result;
    lock = openWithoutSymlinks(lock_path, O_RDWR);
    struct stat information = {};
    if (lock < 0 || ::fstat(lock, &information) != 0 || !S_ISREG(information.st_mode)
        || information.st_nlink != 1)
      goto denied;
    if (::flock(lock, LOCK_EX | LOCK_NB) != 0) {
      if (errno == EWOULDBLOCK || errno == EAGAIN)
        result.failure = AccessFailure::busy;
      goto denied;
    }
    if (!sameRegularFile(lock, lock_path))
      goto denied;
    state = openWithoutSymlinks(state_path, O_RDONLY | O_DIRECTORY);
    if (state < 0 || ::fstat(state, &information) != 0 || information.st_uid != ::geteuid()
        || (information.st_mode & 0777) != 0700)
      goto denied;
    {
      struct timespec acquired = {};
      if (::clock_gettime(CLOCK_MONOTONIC, &acquired) != 0)
        goto denied;
      auto* allocated = new DeviceAccessLease(lock, state, kind,
                   acquired.tv_sec + acquired.tv_nsec / 1000000000.0);
      lock = state = -1;
      auto lease = std::shared_ptr<DeviceAccessLease>(allocated);
      if (!lease->publishState())
        return result;
      result.lease = lease;
      result.failure = AccessFailure::none;
      return result;
    }
  } catch (...) {
    result.failure = AccessFailure::denied;
  }
denied:
  if (state >= 0)
    ::close(state);
  if (lock >= 0)
    ::close(lock);
  return result;
}

bool scannerNameMatchesPolicy(const std::string& name) {
  std::string selected = environment("DCP150C_NETWORK_SCANNER_DEVICE");
  if (!selected.empty() && selected != name)
    return false;
  if (testBackendRequested()) {
    if (name.compare(0, 5, "test:") != 0 || name.size() <= 5 || name.size() > 13)
      return false;
    for (size_t index = 5; index < name.size(); ++index)
      if (name[index] < '0' || name[index] > '9')
        return false;
    return true;
  }
  const std::string prefix = "brother2:bus";
  if (name.compare(0, prefix.size(), prefix) != 0)
    return false;
  size_t separator = name.find(";dev", prefix.size());
  return separator != std::string::npos && decimalRange(name, prefix.size(), separator)
    && decimalRange(name, separator + 4, name.size());
}

bool scannerMatchesPolicy(const std::string& name, const std::string& vendor, const std::string& model) {
  return scannerNameMatchesPolicy(name)
    && (testBackendRequested() || (vendor == "Brother" && model == "DCP-150C"));
}

bool scanGeometryWithinLimits(double resolution, double left, double top, double width, double height) {
  return std::isfinite(resolution) && resolution > 0 && resolution <= maximum_resolution_dpi
    && std::isfinite(left) && std::isfinite(top) && left >= 0 && top >= 0
    && std::isfinite(width) && std::isfinite(height) && width >= 1 && height >= 1
    && width <= maximum_scan_pixels / height;
}

bool parseRequestContentLength(const std::string& value, int maximum_bytes, int& length) {
  if (value.empty() || maximum_bytes < 0)
    return false;
  const int limit = maximum_bytes < maximum_ticket_bytes ? maximum_bytes : maximum_ticket_bytes;
  int parsed = 0;
  for (char character : value) {
    if (character < '0' || character > '9')
      return false;
    const int digit = character - '0';
    if (parsed > limit / 10 || (parsed == limit / 10 && digit > limit % 10))
      return false;
    parsed = parsed * 10 + digit;
  }
  length = parsed;
  return true;
}

} // namespace network_scanner
