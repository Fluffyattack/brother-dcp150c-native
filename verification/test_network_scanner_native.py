# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile native flock checks and patched AirSane with SANE stubs, never USB.

Set DCP150C_AIRSANE_TEST_SOURCE to an unmodified AirSane 0.4.15 source checkout
at ac117f185e06afc7c3557f9839a42d8fd9fd18fa for the optional source tests.
Only temporary copies are patched. No real SANE library or scanner is loaded.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time
import unittest


PROJECT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")
UPSTREAM_HASHES = {
    "CMakeLists.txt": "bbe03f1893d7bad6564a01eafd3e0f983aa573dd308b6a6d2c0c25d2acd5abbd",
    "sanecpp/sanecpp.h": "2cfe16ea34ff274c304a8cbd82baef44a288b21cd6b3af67331e311d68d620e6",
    "sanecpp/sanecpp.cpp": "9cbfc8835b3a6859ecd2bf96f49adcb08831de73a86035d6d05e8d420c115c89",
    "server/server.cpp": "af27b3a418da532c84df069420cfa8571949af4db33626bab010d5377233e133",
    "server/scanner.cpp": "ed3b5bbd0eb9a4a215f564c5191864fe579be477b740cc33589d7fe91bfd6ec1",
    "server/scanjob.cpp": "5a369d7f989a96adfd53b126373692a6dc3f872b2c1c9201d523c100c2f1e379",
    "web/httpserver.cpp": "a6ba692523c73aa72f64788164b43ec0734a5cf258aec5d5a2f0185f868114c6",
}

LEASE_FIXTURE = r'''
#include "device_access.h"
#include <cassert>
#include <iostream>
#include <limits>
#include <string>
int main(int count, char** arguments) {
  assert(count == 2);
  std::string scenario = arguments[1];
  if (scenario == "http_length") {
    int length = -1;
    using network_scanner::parseRequestContentLength;
    assert(parseRequestContentLength("0", 1000000, length) && length == 0);
    assert(parseRequestContentLength("65536", 1000000, length) && length == 65536);
    assert(parseRequestContentLength("00012", 1000000, length) && length == 12);
    for (const char* value : {"", "-1", "+1", "1.5", "Inf", "NaN", "1e3", "1 2", "1\t2", "65537", "999999999999999999999999"})
      assert(!parseRequestContentLength(value, 1000000, length));
    assert(!parseRequestContentLength("100", 99, length));
    return 0;
  }
  if (scenario == "geometry") {
    using network_scanner::scanGeometryWithinLimits;
    assert(scanGeometryWithinLimits(600, 0.1, 0.2, 5000, 7000));
    assert(!scanGeometryWithinLimits(601, 0, 0, 1, 1));
    assert(!scanGeometryWithinLimits(600, 0, 0, 5001, 8000));
    assert(!scanGeometryWithinLimits(300, -1, 0, 100, 100));
    assert(!scanGeometryWithinLimits(300, 0, 0, std::numeric_limits<double>::infinity(), 1));
    assert(!scanGeometryWithinLimits(std::numeric_limits<double>::quiet_NaN(), 0, 0, 1, 1));
    return 0;
  }
  auto access = network_scanner::acquireDeviceAccess(scenario == "session"
    ? network_scanner::AccessKind::session : network_scanner::AccessKind::metadata);
  if (!access) {
    std::cout << (access.failure == network_scanner::AccessFailure::busy ? "busy" : "denied") << std::endl;
    return 0;
  }
  std::cout << "ready" << std::endl;
  if (scenario == "session" || scenario == "metadata") {
    std::string command;
    std::getline(std::cin, command);
  }
  access.lease.reset();
  std::cout << "released" << std::endl;
  return 0;
}
'''

SANE_FIXTURE = r'''
#include "sanecpp/sanecpp.h"
#include "server/scanner.h"
#include "server/scanjob.h"
#include "server/optionsfile.h"
#include "web/httpserver.h"
#include <sane/saneopts.h>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <cstdlib>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <thread>
#include <sys/file.h>
#include <unistd.h>

const char* HttpServer::MIME_TYPE_JPEG = "image/jpeg";
const char* HttpServer::MIME_TYPE_PNG = "image/png";
const char* HttpServer::MIME_TYPE_PDF = "application/pdf";

namespace {
int initialized = 0, exited = 0, opened = 0, closed = 0, active_handles = 0;
int started = 0;
bool fail_initialization = false, fail_open = false, ambiguous = false, pixel_units = false;
bool block_read = false, bad_parameters = false, invalid_length = false, final_eof_with_data = false;
int emitted_rows = 0, available_rows = 3;
double corners[4] = {0, 0, 200, 200};
int resolution = 300;
std::thread::id handle_owner_thread;
std::mutex backend_mutex;
std::condition_variable backend_condition;
int pause_point = 0;
bool backend_entered = false, backend_released = false;
std::atomic<int> cancellation_calls{0};

void pause_backend(int point) {
  if (pause_point != point)
    return;
  std::unique_lock<std::mutex> lock(backend_mutex);
  backend_entered = true;
  backend_condition.notify_all();
  backend_condition.wait(lock, [] { return backend_released; });
}
SANE_Option_Descriptor options[9] = {};
const SANE_Range millimetres = {0, SANE_FIX(200), 0};
const SANE_Range pixels = {0, 2400, 0};
const SANE_Range depth = {8, 8, 0};
const SANE_Word resolutions[] = {5, 75, 150, 300, 600, 1200};
const SANE_String_Const modes[] = {"True Gray", "24bit Color", nullptr};
const SANE_String_Const sources[] = {"FlatBed", nullptr};
const SANE_Device target = {"brother2:bus1;dev7", "Brother", "DCP-150C", "flatbed"};
const SANE_Device second = {"brother2:bus1;dev8", "Brother", "DCP-150C", "flatbed"};
const SANE_Device other_model = {"brother2:bus1;dev9", "Brother", "DCP-151C", "flatbed"};
const SANE_Device other_backend = {"escl:remote", "Brother", "DCP-150C", "flatbed"};
const SANE_Device network = {"brother2:net1;dev0", "Brother", "DCP-150C", "flatbed"};
const SANE_Device virtual_device = {"test:0", "Noname", "frontend-tester", "virtual"};
const SANE_Device* listed[] = {&target, &other_model, &other_backend, &network, &virtual_device, nullptr, nullptr};

bool marker_exists() {
  return bool(std::ifstream(std::string(std::getenv("DCP150C_NETWORK_SCANNER_STATE_DIRECTORY")) + "/lease.json"));
}
void protected_call() {
  assert(marker_exists());
  std::ifstream marker(std::string(std::getenv("DCP150C_NETWORK_SCANNER_STATE_DIRECTORY")) + "/lease.json");
  std::string content((std::istreambuf_iterator<char>(marker)), std::istreambuf_iterator<char>());
  assert(content.find("\"process_id\":" + std::to_string(::getpid()) + ",") != std::string::npos);
  int descriptor = ::open(std::getenv("DCP150C_LOCK_PATH"), O_RDWR | O_CLOEXEC);
  assert(descriptor >= 0);
  assert(::flock(descriptor, LOCK_EX | LOCK_NB) != 0);
  assert(errno == EAGAIN || errno == EWOULDBLOCK);
  ::close(descriptor);
}
void initialize_options() {
  const char* names[] = {"", SANE_NAME_SCAN_MODE, SANE_NAME_SCAN_RESOLUTION, SANE_NAME_SCAN_SOURCE,
    SANE_NAME_SCAN_TL_X, SANE_NAME_SCAN_TL_Y, SANE_NAME_SCAN_BR_X, SANE_NAME_SCAN_BR_Y, SANE_NAME_BIT_DEPTH};
  for (int index = 0; index < 9; ++index) {
    options[index].name = names[index];
    options[index].title = names[index];
    options[index].desc = names[index];
    options[index].type = SANE_TYPE_INT;
    options[index].unit = SANE_UNIT_NONE;
    options[index].size = sizeof(SANE_Word);
    options[index].cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;
    options[index].constraint_type = SANE_CONSTRAINT_NONE;
  }
  options[1].type = options[3].type = SANE_TYPE_STRING;
  options[1].size = options[3].size = 64;
  options[1].constraint_type = options[3].constraint_type = SANE_CONSTRAINT_STRING_LIST;
  options[1].constraint.string_list = modes;
  options[3].constraint.string_list = sources;
  options[2].unit = SANE_UNIT_DPI;
  options[2].constraint_type = SANE_CONSTRAINT_WORD_LIST;
  options[2].constraint.word_list = resolutions;
  for (int index = 4; index < 8; ++index) {
    options[index].type = pixel_units ? SANE_TYPE_INT : SANE_TYPE_FIXED;
    options[index].unit = pixel_units ? SANE_UNIT_PIXEL : SANE_UNIT_MM;
    options[index].constraint_type = SANE_CONSTRAINT_RANGE;
    options[index].constraint.range = pixel_units ? &pixels : &millimetres;
  }
  options[8].constraint_type = SANE_CONSTRAINT_RANGE;
  options[8].constraint.range = &depth;
}
std::string ticket(int dpi = 300, const std::string& width = "100") {
  return "<scan:ScanSettings><scan:Intent>Photo</scan:Intent><scan:XResolution>" + std::to_string(dpi)
    + "</scan:XResolution><scan:YResolution>" + std::to_string(dpi)
    + "</scan:YResolution><scan:ColorMode>RGB24</scan:ColorMode><scan:InputSource>Platen</scan:InputSource>"
      "<scan:DocumentFormat>image/png</scan:DocumentFormat><scan:ContentRegionUnits>escl:ThreeHundredthsOfInches</scan:ContentRegionUnits>"
      "<scan:XOffset>10.25</scan:XOffset><scan:YOffset>20.75</scan:YOffset><scan:Width>" + width
    + "</scan:Width><scan:Height>100</scan:Height></scan:ScanSettings>";
}
}

extern "C" {
SANE_Status sane_init(SANE_Int*, SANE_Auth_Callback) {
  protected_call(); ++initialized; initialize_options();
  return fail_initialization ? SANE_STATUS_IO_ERROR : SANE_STATUS_GOOD;
}
void sane_exit() { protected_call(); assert(active_handles == 0); ++exited; }
SANE_Status sane_get_devices(const SANE_Device*** result, SANE_Bool) {
  protected_call();
  listed[5] = ambiguous ? &second : nullptr;
  *result = listed;
  return SANE_STATUS_GOOD;
}
SANE_Status sane_open(SANE_String_Const, SANE_Handle* result) {
  protected_call(); ++opened; ++active_handles; emitted_rows = 0;
  handle_owner_thread = std::this_thread::get_id();
  *result = new int(1);
  pause_backend(1);
  return fail_open ? SANE_STATUS_IO_ERROR : SANE_STATUS_GOOD;
}
void sane_close(SANE_Handle handle) {
  protected_call(); assert(std::this_thread::get_id() == handle_owner_thread);
  ++closed; --active_handles; delete static_cast<int*>(handle);
}
const SANE_Option_Descriptor* sane_get_option_descriptor(SANE_Handle, SANE_Int index) {
  protected_call(); return index >= 0 && index < 9 ? &options[index] : nullptr;
}
SANE_Status sane_control_option(SANE_Handle, SANE_Int index, SANE_Action action, void* value, SANE_Int* information) {
  protected_call();
  if (information) *information = 0;
  if (action == SANE_ACTION_GET_VALUE) {
    pause_backend(2);
    if (index == 1) std::strcpy(static_cast<char*>(value), "24bit Color");
    else if (index == 3) std::strcpy(static_cast<char*>(value), "FlatBed");
    else if (index >= 4 && index < 8) *static_cast<SANE_Word*>(value) = pixel_units
      ? static_cast<SANE_Word>(corners[index - 4]) : SANE_FIX(corners[index - 4]);
    else *static_cast<SANE_Word*>(value) = index == 2 ? resolution : 8;
  } else if (action == SANE_ACTION_SET_VALUE) {
    if (index == 2 && *static_cast<SANE_Word*>(value) == 301)
      return SANE_STATUS_INVAL;
    if (index == 2) resolution = *static_cast<SANE_Word*>(value);
    if (index >= 4 && index < 8) corners[index - 4] = pixel_units
      ? *static_cast<SANE_Word*>(value) : SANE_UNFIX(*static_cast<SANE_Word*>(value));
  }
  return SANE_STATUS_GOOD;
}
SANE_Status sane_start(SANE_Handle) { protected_call(); ++started; return SANE_STATUS_GOOD; }
SANE_Status sane_get_parameters(SANE_Handle, SANE_Parameters* parameters) {
  protected_call();
  *parameters = {SANE_FRAME_RGB, SANE_TRUE, 24, 8, 3, 8};
  if (bad_parameters) { parameters->pixels_per_line = 100000; parameters->lines = 100000; }
  return SANE_STATUS_GOOD;
}
SANE_Status sane_read(SANE_Handle, SANE_Byte* bytes, SANE_Int maximum, SANE_Int* length) {
  protected_call();
  pause_backend(3);
  if (block_read) { std::cout << "blocked" << std::endl; char command; std::cin.get(command); }
  if (invalid_length) { *length = maximum + 1; return SANE_STATUS_GOOD; }
  if (emitted_rows >= available_rows) { *length = 0; return SANE_STATUS_EOF; }
  *length = maximum; std::memset(bytes, 42 + emitted_rows, maximum); ++emitted_rows;
  return final_eof_with_data && emitted_rows == available_rows ? SANE_STATUS_EOF : SANE_STATUS_GOOD;
}
void sane_cancel(SANE_Handle) {
  protected_call(); assert(std::this_thread::get_id() == handle_owner_thread); ++cancellation_calls;
}
SANE_String_Const sane_strstatus(SANE_Status) { return "stub status"; }
}

void verify_cooperative_cancellation(const std::string& scenario, const std::string& name) {
  Scanner scanner(sanecpp::device_info{name, "Brother", "DCP-150C", "flatbed"});
  OptionsFile configuration(std::string(std::getenv("DCP150C_NETWORK_SCANNER_STATE_DIRECTORY")) + "/options.conf");
  assert(scanner.initWithOptions(configuration));
  auto job = scanner.createJobFromScanSettingsXml(ticket(), false);
  if (scenario == "cancel_pending") {
    job->cancel();
    assert(!job->beginTransfer() && started == 0 && !marker_exists());
    assert(job->state() == ScanJob::canceled && job->statusString() == "Canceled");
  } else {
    pause_point = scenario == "cancel_open" ? 1 : scenario == "cancel_options" ? 2 : 3;
    std::atomic<bool> cancellation_finished{false};
    bool begin_result = false;
    std::thread transfer([&] {
      begin_result = job->beginTransfer();
      if (begin_result) {
        std::ostringstream output;
        job->finishTransfer(output);
      }
    });
    {
      std::unique_lock<std::mutex> lock(backend_mutex);
      assert(backend_condition.wait_for(lock, std::chrono::seconds(3), [] { return backend_entered; }));
    }
    const int closes_before_cancel = closed;
    const int cancels_before_cancel = cancellation_calls.load();
    std::thread cancellation([&] { job->cancel(); cancellation_finished = true; });
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (job->state() != ScanJob::canceled && std::chrono::steady_clock::now() < deadline)
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    assert(job->state() == ScanJob::canceled && job->statusString() == "Canceled");
    assert(!job->isProcessing() && job->isFinished());
    assert(marker_exists() && active_handles == 1 && closed == closes_before_cancel);
    assert(cancellation_calls == cancels_before_cancel && !cancellation_finished);
    {
      std::lock_guard<std::mutex> lock(backend_mutex);
      backend_released = true;
      backend_condition.notify_all();
    }
    transfer.join();
    cancellation.join();
    assert(cancellation_finished && job->state() == ScanJob::canceled && !marker_exists());
    assert(closed == closes_before_cancel + 1 && cancellation_calls == cancels_before_cancel + 1);
    if (pause_point != 3)
      assert(!begin_result && started == 0);
    pause_point = 0;
  }
  auto following = scanner.createJobFromScanSettingsXml(ticket(), false);
  assert(following->beginTransfer());
  std::ostringstream image;
  following->finishTransfer(image);
  assert(following->state() == ScanJob::completed && !marker_exists() && active_handles == 0);
}

int main(int count, char** arguments) {
  assert(count == 2);
  const std::string scenario = arguments[1];
  const std::string name = std::getenv("DCP150C_NETWORK_SCANNER_ALLOW_TEST_BACKEND") ? "test:0" : target.name;
  if (scenario == "job_capacity") {
    Scanner scanner(sanecpp::device_info{name, "Brother", "DCP-150C", "flatbed"});
    OptionsFile configuration(std::string(std::getenv("DCP150C_NETWORK_SCANNER_STATE_DIRECTORY")) + "/options.conf");
    assert(scanner.initWithOptions(configuration));
    std::vector<std::shared_ptr<ScanJob>> jobs;
    for (int index = 0; index < 16; ++index) {
      auto job = scanner.createJobFromScanSettingsXml(ticket(), false);
      assert(job); jobs.push_back(job);
    }
    assert(!scanner.createJobFromScanSettingsXml(ticket(), false));
    assert(started == 0 && opened == 1 && !marker_exists());
    jobs.clear();
    assert(scanner.purgeJobs(-1) == 16);
    assert(scanner.createJobFromScanSettingsXml(ticket(), false));
    assert(started == 0 && opened == 1 && !marker_exists());
  } else if (scenario.compare(0, 7, "cancel_") == 0) {
    verify_cooperative_cancellation(scenario, name);
  } else if (scenario == "initialization_failure") {
    fail_initialization = true;
    { sanecpp::init runtime; assert(runtime.status() == SANE_STATUS_IO_ERROR); }
    SANE_Status status = SANE_STATUS_GOOD;
    assert(!sanecpp::open(name, &status)); assert(status == SANE_STATUS_IO_ERROR);
    assert(initialized == 2 && opened == 0 && exited == 0 && !marker_exists());
  } else if (scenario == "metadata") {
    { sanecpp::init runtime; assert(runtime.status() == SANE_STATUS_GOOD); assert(!marker_exists());
      SANE_Status status; auto devices = sanecpp::enumerate_devices(true, &status);
      assert(status == SANE_STATUS_GOOD && devices.size() == 1 && devices[0].name == name && !marker_exists());
      { auto handle = sanecpp::open(name); assert(handle && marker_exists());
        assert(!sanecpp::open(name));
        sanecpp::option_set options(handle); handle.reset(); assert(marker_exists());
      } assert(!marker_exists());
    }
    assert(initialized == 1 && opened == 1 && closed == 1 && exited == 1 && !marker_exists());
  } else if (scenario == "deferred_cleanup") {
    auto runtime = std::unique_ptr<sanecpp::init>(new sanecpp::init);
    assert(runtime->status() == SANE_STATUS_GOOD);
    int busy = ::open(std::getenv("DCP150C_LOCK_PATH"), O_RDWR | O_CLOEXEC);
    assert(busy >= 0 && ::flock(busy, LOCK_EX | LOCK_NB) == 0);
    runtime.reset(); assert(exited == 0); ::close(busy);
    { sanecpp::init replacement; assert(replacement.status() == SANE_STATUS_GOOD); assert(exited == 1); }
    assert(initialized == 2 && exited == 2 && !marker_exists());
  } else if (scenario == "filter") {
    SANE_Status status = SANE_STATUS_GOOD;
    assert(!sanecpp::open("brother2:net1;dev0", &status)); assert(status == SANE_STATUS_ACCESS_DENIED);
    assert(!sanecpp::open("escl:remote")); assert(initialized == 0);
    assert(!sanecpp::open(other_model.name, &status)); assert(status == SANE_STATUS_ACCESS_DENIED && opened == 0);
    ambiguous = true;
    auto devices = sanecpp::enumerate_devices(true, &status);
    assert(devices.empty() && status == SANE_STATUS_INVAL && !marker_exists());
  } else if (scenario == "open_failure") {
    fail_open = true; SANE_Status status = SANE_STATUS_GOOD;
    assert(!sanecpp::open(name, &status)); assert(status == SANE_STATUS_IO_ERROR);
    assert(opened == 1 && closed == 1 && exited == 1 && active_handles == 0 && !marker_exists());
  } else if (scenario == "session" || scenario == "blocked_read" || scenario == "bad_parameters" || scenario == "invalid_length") {
    block_read = scenario == "blocked_read"; bad_parameters = scenario == "bad_parameters";
    invalid_length = scenario == "invalid_length";
    { sanecpp::session session(name); assert(session.status() == SANE_STATUS_GOOD && marker_exists());
      session.start();
      if (bad_parameters) assert(session.status() == SANE_STATUS_INVAL);
      else { std::vector<char> bytes(24); session.read(bytes);
        assert(session.status() == (invalid_length ? SANE_STATUS_IO_ERROR : SANE_STATUS_GOOD));
      }
    }
    assert(opened == 1 && closed == 1 && exited == 1 && !marker_exists());
  } else {
    pixel_units = scenario == "pixel_roi";
    available_rows = scenario == "missing_rows" ? 2 : scenario == "extra_rows" ? 4 : 3;
    final_eof_with_data = scenario == "final_eof_with_data";
    Scanner scanner(sanecpp::device_info{name, "Brother", "DCP-150C", "flatbed"});
    OptionsFile configuration(std::string(std::getenv("DCP150C_NETWORK_SCANNER_STATE_DIRECTORY")) + "/options.conf");
    assert(scanner.initWithOptions(configuration)); assert(scanner.maxResDpi() == 600);
    assert(!marker_exists());
    auto job = scanner.createJobFromScanSettingsXml(ticket(scenario == "oversized_resolution" ? 1200 : scenario == "unsupported_resolution" ? 301 : 300,
      scenario == "oversized_area" ? "1000000" : "100"), false);
    if (scenario == "oversized_resolution" || scenario == "oversized_area") {
      assert(job->state() == ScanJob::aborted && !job->beginTransfer()); assert(opened == 1);
    } else if (scenario == "unsupported_resolution") {
      assert(!job->beginTransfer() && job->state() == ScanJob::aborted && started == 0);
    } else {
      assert(job->beginTransfer());
      if (pixel_units) { assert(corners[0] == 10 && corners[1] == 21); }
      else { assert(std::abs(corners[0] - 10.25 * 25.4 / 300) < 0.0001);
        assert(std::abs(corners[1] - 20.75 * 25.4 / 300) < 0.0001); }
      std::ostringstream image; job->finishTransfer(image);
      assert(job->state() == ((available_rows == 3) ? ScanJob::completed : ScanJob::aborted));
    }
    assert(!marker_exists() && active_handles == 0);
  }
  std::cout << "passed" << std::endl;
}
'''


class IsolatedEnvironment(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dcp150c-native-network-")
        self.root = Path(self.temporary.name)
        self.lock = self.root / "device.lock"
        self.lock.touch()
        self.lock.chmod(0o600)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.configuration = self.root / "sane"
        self.configuration.mkdir(mode=0o700)
        backend_configuration = self.configuration / "dll.conf"
        backend_configuration.write_text("test\n", encoding="ascii")
        backend_configuration.chmod(0o600)
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("DCP150C_") and key != "SANE_CONFIG_DIR"}
        self.environment.update(DCP150C_LOCK_PATH=str(self.lock),
                                DCP150C_NETWORK_SCANNER_STATE_DIRECTORY=str(self.state),
                                SANE_CONFIG_DIR=str(self.configuration))

    def tearDown(self):
        self.temporary.cleanup()

    def run_fixture(self, executable, scenario, **changes):
        result = subprocess.run([str(executable), scenario], env={**self.environment, **changes},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, scenario + "\n" + result.stdout + result.stderr)
        return result

    def ready_line(self, process):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5), "The native fixture did not respond")
            return process.stdout.readline().strip()


@unittest.skipUnless(COMPILER, "A C++ compiler is required for native locking checks")
class DeviceAccessNativeTests(IsolatedEnvironment):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="dcp150c-network-lease-build-")
        directory = Path(cls.build.name)
        source = directory / "fixture.cpp"
        source.write_text(LEASE_FIXTURE)
        cls.executable = directory / "lease-fixture"
        subprocess.run([COMPILER, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pthread",
                        "-I" + str(PROJECT / "src/network_scanner"),
                        str(PROJECT / "src/network_scanner/device_access.cpp"), str(source),
                        "-o", str(cls.executable)], check=True, capture_output=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def test_real_process_lock_marker_lifetime_and_atomic_removal(self):
        for kind in ("metadata", "session"):
            with self.subTest(kind=kind):
                process = subprocess.Popen([str(self.executable), kind], env=self.environment,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(self.ready_line(process), "ready")
                    marker = self.state / "lease.json"
                    record = json.loads(marker.read_text())
                    self.assertEqual(record["process_id"], process.pid)
                    self.assertEqual(record["kind"], kind)
                    self.assertGreater(record["acquired_monotonic"], time.monotonic() - 10)
                    self.assertLessEqual(record["acquired_monotonic"], time.monotonic())
                    self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
                    before = marker.read_bytes()
                    competing = self.run_fixture(self.executable, "probe")
                    self.assertEqual(competing.stdout.strip(), "busy")
                    self.assertEqual(marker.read_bytes(), before)
                    output, error = process.communicate("release\n", timeout=5)
                    self.assertEqual(process.returncode, 0, error)
                    self.assertEqual(output.strip(), "released")
                    self.assertFalse(marker.exists())
                    self.assertEqual(list(self.state.iterdir()), [])
                    self.assertEqual(self.run_fixture(self.executable, "probe").stdout, "ready\nreleased\n")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()

    def test_unsafe_or_missing_lock_and_state_paths_fail_closed(self):
        original = self.lock.with_name("saved.lock")
        self.lock.rename(original)
        self.assertEqual(self.run_fixture(self.executable, "probe").stdout.strip(), "denied")
        self.lock.symlink_to(original)
        self.assertEqual(self.run_fixture(self.executable, "probe").stdout.strip(), "denied")
        self.lock.unlink()
        os.mkfifo(self.lock)
        self.assertEqual(self.run_fixture(self.executable, "probe").stdout.strip(), "denied")
        self.lock.unlink()
        original.rename(self.lock)
        self.state.chmod(0o755)
        self.assertEqual(self.run_fixture(self.executable, "probe").stdout.strip(), "denied")
        self.state.chmod(0o700)
        renamed = self.state.with_name("real-state")
        self.state.rename(renamed)
        self.state.symlink_to(renamed)
        self.assertEqual(self.run_fixture(self.executable, "probe").stdout.strip(), "denied")

    def test_failed_marker_publication_preserves_existing_state_and_unlocks(self):
        target = self.root / "untouched"
        target.write_text("Original bytes")
        (self.state / "lease.json").symlink_to(target)
        self.assertEqual(self.run_fixture(self.executable, "probe").stdout.strip(), "denied")
        self.assertEqual(target.read_text(), "Original bytes")
        with self.lock.open("r+") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_virtual_backend_flag_requires_only_test_in_an_isolated_sane_configuration(self):
        changes = {"DCP150C_NETWORK_SCANNER_ALLOW_TEST_BACKEND": "1"}
        self.assertEqual(self.run_fixture(self.executable, "probe", **changes).stdout, "ready\nreleased\n")
        (self.configuration / "dll.conf").write_text("test\nbrother2\n")
        self.assertEqual(self.run_fixture(self.executable, "probe", **changes).stdout.strip(), "denied")
        (self.configuration / "dll.conf").write_text("test\n")
        (self.configuration / "dll.d").mkdir()
        self.assertEqual(self.run_fixture(self.executable, "probe", **changes).stdout.strip(), "denied")
        self.assertEqual(self.run_fixture(self.executable, "probe", **changes,
                                         SANE_CONFIG_DIR="/etc/sane.d").stdout.strip(), "denied")

    def test_resolution_area_and_nonfinite_values_have_native_bounds(self):
        self.run_fixture(self.executable, "geometry")

    def test_http_lengths_are_bounded_decimal_integers(self):
        self.run_fixture(self.executable, "http_length")


@unittest.skipUnless(COMPILER and os.environ.get("DCP150C_AIRSANE_TEST_SOURCE"),
                     "Set DCP150C_AIRSANE_TEST_SOURCE to the pinned AirSane source for SANE-stub checks")
class PatchedSaneNativeTests(IsolatedEnvironment):
    @classmethod
    def setUpClass(cls):
        cls.original = Path(os.environ["DCP150C_AIRSANE_TEST_SOURCE"])
        for name, expected in UPSTREAM_HASHES.items():
            actual = hashlib.sha256((cls.original / name).read_bytes()).hexdigest()
            if actual != expected:
                raise AssertionError("The AirSane source differs from the pinned commit: " + name)
        cls.build = tempfile.TemporaryDirectory(prefix="dcp150c-patched-sane-build-")
        cls.directory = Path(cls.build.name)
        source = cls.directory / "airsane"
        shutil.copytree(cls.original, source, ignore=shutil.ignore_patterns(".git"))
        subprocess.run(["git", "apply", "--check", str(PROJECT / "patches/airsane-shared-device-access.patch")],
                       cwd=source, check=True, capture_output=True, timeout=10)
        subprocess.run(["git", "apply", str(PROJECT / "patches/airsane-shared-device-access.patch")],
                       cwd=source, check=True, capture_output=True, timeout=10)
        shutil.copytree(PROJECT / "src/network_scanner", source / "network_scanner")
        fixture = cls.directory / "sane-fixture.cpp"
        fixture.write_text(SANE_FIXTURE)
        cls.executable = cls.directory / "sane-fixture"
        sources = ["sanecpp/sanecpp.cpp", "network_scanner/device_access.cpp", "server/scanner.cpp",
                   "server/scanjob.cpp", "server/optionsfile.cpp", "basic/uuid.cpp", "basic/dictionary.cpp",
                   "basic/workerthread.cpp", "imageformats/imageencoder.cpp", "imageformats/jpegencoder.cpp",
                   "imageformats/pngencoder.cpp", "imageformats/pdfencoder.cpp"]
        subprocess.run([COMPILER, "-std=c++11", "-Wall", "-Wextra", "-pthread", "-I" + str(source),
                        *[str(source / name) for name in sources], str(fixture), "-lpng", "-ljpeg", "-latomic",
                        "-o", str(cls.executable)], check=True, capture_output=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        for name, expected in UPSTREAM_HASHES.items():
            assert hashlib.sha256((cls.original / name).read_bytes()).hexdigest() == expected
        cls.build.cleanup()

    def test_metadata_handles_share_lease_and_idle_runtime_releases_lock(self):
        self.run_fixture(self.executable, "metadata")

    def test_session_holds_lock_through_options_start_read_cancel_close_and_exit(self):
        self.run_fixture(self.executable, "session")

    def test_pending_cancellation_prevents_start_and_allows_a_following_scan(self):
        self.run_fixture(self.executable, "cancel_pending")

    def test_cancellation_during_blocked_open_keeps_handle_alive_until_worker_cleanup(self):
        self.run_fixture(self.executable, "cancel_open")

    def test_cancellation_during_options_dump_keeps_option_storage_alive(self):
        self.run_fixture(self.executable, "cancel_options")

    def test_cancellation_during_blocked_read_preserves_lock_and_finishes_on_worker(self):
        self.run_fixture(self.executable, "cancel_read")

    def test_sixteen_pending_jobs_bound_threads_and_purge_restores_capacity_without_scanning(self):
        self.run_fixture(self.executable, "job_capacity")

    def test_init_failure_is_propagated_and_partial_open_is_cleaned_under_lease(self):
        self.run_fixture(self.executable, "initialization_failure")
        self.run_fixture(self.executable, "open_failure")

    def test_model_backend_and_ambiguity_filters_precede_hardware_open(self):
        self.run_fixture(self.executable, "filter")

    def test_deferred_exit_is_traced_and_retried_only_under_a_new_lease(self):
        result = self.run_fixture(self.executable, "deferred_cleanup")
        self.assertIn("phase=cleanup_deferred status=busy", result.stderr)

    def test_explicit_virtual_backend_stays_within_isolated_configuration(self):
        self.run_fixture(self.executable, "metadata", DCP150C_NETWORK_SCANNER_ALLOW_TEST_BACKEND="1")

    def test_roi_preserves_fractional_millimetres_and_rounds_pixel_coordinates(self):
        self.run_fixture(self.executable, "millimetre_roi")
        self.run_fixture(self.executable, "pixel_roi")

    def test_incomplete_and_excess_rows_abort_while_complete_final_eof_data_succeeds(self):
        for scenario in ("missing_rows", "extra_rows", "final_eof_with_data"):
            with self.subTest(scenario=scenario):
                self.run_fixture(self.executable, scenario)

    def test_capabilities_tickets_and_returned_backend_parameters_enforce_limits(self):
        for scenario in ("oversized_resolution", "oversized_area", "unsupported_resolution", "bad_parameters", "invalid_length"):
            with self.subTest(scenario=scenario):
                self.run_fixture(self.executable, scenario)

    def test_blocked_backend_keeps_marker_and_lock_until_process_termination(self):
        process = subprocess.Popen([str(self.executable), "blocked_read"], env=self.environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(self.ready_line(process), "blocked")
            record = json.loads((self.state / "lease.json").read_text())
            self.assertEqual(record["kind"], "session")
            self.assertEqual(record["process_id"], process.pid)
            with self.lock.open("r+") as descriptor:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                process.terminate()
                process.wait(timeout=5)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue((self.state / "lease.json").exists(), "Termination must not pretend backend cleanup completed")
            self.run_fixture(self.executable, "session")
            self.assertFalse((self.state / "lease.json").exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


if __name__ == "__main__":
    unittest.main()
