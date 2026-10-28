# Native scanner setup

The Control Center uses SANE, the Linux scanner interface. Its scanner operations
share the same device lock as printing and ink polling. Opening the website never
starts a scan. A connection check is an explicit action; each scan needs a separate
request.

An optional source build is provided for the DCP-150C's older `brscan2` protocol.
It combines the GPL Brother backend maintained in
[simonfojtu/brscan2](https://github.com/simonfojtu/brscan2) with the portable decoder
and color library from [dmikushin/brscan](https://github.com/dmikushin/brscan).
Both revisions and source archive checksums are pinned in the build script.
The newer-model USB backend from the second project is not used for this printer.

The build keeps source and license notices alongside the libraries. No original
x86 scanner libraries are installed. Additional vendor color correction is
disabled in the initial model configuration; the scanner's decoded color output
is used directly. This setting avoids representing an unverified calibration as
available.

## Build and install on the printer Pi

Install the main project first, including its service account and USB permissions.
Then build the scanner component on the same Pi that will use it:

```sh
sudo apt install build-essential libusb-dev sane-utils
python3 scripts/build-scanner.py
sudo python3 scripts/install-scanner.py
```

The build uses one compiler process at a time. The source cache requires roughly
60 MB for the pinned downloads. Only selected source files are extracted; upstream
binary files, test captures and other build artifacts are excluded.

Installation adds one `brother2` entry under `/etc/sane.d/dll.d/`. It installs
project libraries under `/usr/lib/dcp150c-native/scanner/` and preserves unrelated
SANE configuration. An existing foreign `brother2` backend is preserved and causes
installation to stop with an explanation. Installation does not enumerate USB
devices or run a scanner operation.

With the printer switched on, use **Check scanner connection** in the Control
Center. The available modes, resolutions and scan area come from the connected
device. A device that has not been checked remains unavailable in the interface.
The default document setting is A4 at 300 dots per inch, when the device advertises
that setting. Resolution is limited to 600 dots per inch and decoded image size to
40 million pixels to fit the printer Pi's resources.

## Verification

The web scanner service is checked against SANE's isolated virtual test backend.
These tests exercise a real image acquisition pipeline, invalid settings, and the
shared printer lock while loading no physical-device backend:

```sh
python3 -m unittest verification.test_scanner_service -v
```

Virtual tests do not establish physical scanner support. The optional native
backend must still be checked with the actual DCP-150C: acquire an image, cancel a
scan, and print afterwards. The interface reports successful hardware acquisition
only after receiving and validating an image from the selected device.
