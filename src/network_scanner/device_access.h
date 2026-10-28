// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef DCP150C_NETWORK_SCANNER_DEVICE_ACCESS_H
#define DCP150C_NETWORK_SCANNER_DEVICE_ACCESS_H

#include <memory>
#include <string>
#include <sys/types.h>

namespace network_scanner {

enum class AccessKind { metadata, session };
enum class AccessFailure { none, busy, denied };

class DeviceAccessLease;
struct AccessResult {
  AccessResult(std::shared_ptr<DeviceAccessLease> lease = {},
               AccessFailure failure = AccessFailure::denied)
    : lease(lease), failure(failure) {}
  std::shared_ptr<DeviceAccessLease> lease;
  AccessFailure failure;
  explicit operator bool() const { return bool(lease); }
};

// No device access occurs until this explicit operation. Independent callers
// always acquire independent flock descriptors; nested calls borrow a lease.
AccessResult acquireDeviceAccess(AccessKind) noexcept;
void reportDeviceAccessFailure(const char* phase, AccessFailure) noexcept;

class DeviceAccessLease {
public:
  ~DeviceAccessLease();
  DeviceAccessLease(const DeviceAccessLease&) = delete;
  DeviceAccessLease& operator=(const DeviceAccessLease&) = delete;
  AccessKind kind() const { return access_kind; }

private:
  friend AccessResult acquireDeviceAccess(AccessKind) noexcept;
  DeviceAccessLease(int lock_descriptor, int state_descriptor, AccessKind,
                    double acquired_monotonic);
  bool publishState() noexcept;
  int lock_descriptor;
  int state_descriptor;
  AccessKind access_kind;
  double acquired_monotonic;
  bool published = false;
  dev_t marker_device = 0;
  ino_t marker_inode = 0;
};

// The test backend additionally requires an isolated SANE_CONFIG_DIR containing
// only the test backend. Production accepts only this model's local USB name.
bool scannerNameMatchesPolicy(const std::string& name);
bool scannerMatchesPolicy(const std::string& name, const std::string& vendor,
                          const std::string& model);
bool scanGeometryWithinLimits(double resolution, double left, double top,
                              double width, double height);
bool parseRequestContentLength(const std::string& value, int maximum_bytes, int& length);
constexpr int maximum_resolution_dpi = 600;
constexpr unsigned long long maximum_scan_pixels = 40000000;
constexpr int maximum_ticket_bytes = 64 * 1024;

} // namespace network_scanner
#endif
