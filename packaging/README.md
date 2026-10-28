# Raspberry Pi OS installation

The installer supports Raspberry Pi OS with systemd and CUPS 2.x. The status reader uses Python's standard library; document controls use the optional packages described below. Native printing additionally needs Ghostscript and a `bh7filter` built for the Pi's CPU, together with the matching `brprintconf_dcp150c` configuration tool. An A4/plain-paper/Normal test page has been physically printed on a Pi Zero 2 W. The [hardware validation results](../README.md#hardware-validation) describe the tested scope.

Install the operating-system dependencies:

```sh
sudo apt update
sudo apt install build-essential python3 cups cups-client cups-filters ghostscript avahi-daemon
```

## Dashboard only

```sh
sudo scripts/install.sh --dashboard-only
```

Open `http://<pi-address>:8080/`. Existing CUPS queues are preserved. The service runs as the dedicated `dcp150c` user, with USB access limited to the DCP-150C device by a udev rule. Ink information that has not been established from the device remains unknown.

To monitor an existing queue, add `--queue <existing-queue-name>` to the dashboard-only command. This selects that queue in the dashboard without changing the queue itself.

Dashboard settings go in `/etc/dcp150c-native/dashboard.env`. The installer preserves this file when it already exists. A fresh installation enables the verified cartridge queries:

```ini
DCP150C_MODEL_STATUS=1
```

Ink polling requires this project's CUPS backend, no queued print jobs, an identified DCP-150C and exclusive access to the shared device lock. Each of the two response phases allows ten seconds because the printer responds after nearly eight seconds. A full reading usually takes about sixteen seconds; browsers continue to receive cached snapshots during that time.

When upgrading from an earlier preview, change an existing `DCP150C_MODEL_STATUS=0` to `1` to enable ink readings. Set it to `0` for basic queue and connection monitoring. The command-line equivalents are `--enable-model-status` and `--disable-model-status`. `--timeout` sets the limit for each response phase; the default is ten seconds.

Optional settings in the same file include:

```ini
DCP150C_QUEUE=DCP150C-Native
DCP150C_DEVICE=/dev/usb/lp0
```

Restart after changes:

```sh
sudo systemctl restart dcp150c-dashboard
systemctl status dcp150c-dashboard
```

## Control Center documents and password

The status page remains usable without document-processing packages. For PDF/image uploads, page editing, previews and local OCR, install:

```sh
sudo apt install python3-pil python3-pypdf poppler-utils tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng
sudo env PYTHONPATH=/usr/lib/dcp150c-native python3 -m dcp150c.access_control
sudo systemctl restart dcp150c-dashboard
```

Choose your own password of at least twelve characters. The command stores a salted scrypt password hash in `/etc/dcp150c-native/control-center-password.json`; it grants the service group read access. There is no default password. Without a valid password configuration, personal documents and actions remain inaccessible. Restarting closes existing browser sessions; signing out closes the current session.

Systemd creates `/var/lib/dcp150c-control-center` for the dedicated service account, with private directory permissions. It contains documents, previews, OCR text and the settings/job database. Temporary documents expire after 24 hours by default; kept documents remain until explicitly deleted. Limits are configurable in the authenticated settings view. Uploads are limited to 20 MiB and 25 pages. OCR runs one page at a time with a single worker and CPU thread. The service has a 320 MiB memory ceiling; very large pages can be refused.

For an uninstalled status-only run, pass `--disable-control-center`. For a development Control Center, use `--state-directory /absolute/private/directory` and `--password-file /absolute/password-hash.json`. Keep that directory out of source control. Changing dependencies requires restarting the service to refresh capability information.

Requests that change data require a browser session and its request-verification token. Personal downloads are protected too. Publishing through an HTTPS reverse proxy is supported; the proxy should set `X-Forwarded-Proto: https` so browser session cookies are marked Secure. Restrict access to your own users. A temporary tunnel address can change when its tunnel is recreated.

Settings exports contain profiles and retention/storage settings. They exclude credentials, browser sessions and document contents. Normal software removal preserves personal documents; remove `/var/lib/dcp150c-control-center` separately only when those files are no longer needed.

Scan and copy controls require a compatible native SANE backend. See the [native scanner build instructions](../docs/scanner-setup.md); installing SANE alone does not establish model support. A connection check is an explicit browser action. Startup, page refresh and installation never start scanning or print a test page. Removal also removes the scanner's exact owned library link and registration, while preserving foreign entries.

The [German usage guide](../docs/control-center-usage.de.md) explains the browser workflows.

The memory-card reader uses Python's standard library and opens only block devices under USB `04f9:01cf`, with group-read access from the supplied udev rule. It parses FAT12/16/32 directly without mounting the card or using a privileged helper. It refuses writable mounts, including a read-only bind mount backed by a writable filesystem. The normal document dependencies above are needed for import and preview. `dosfstools` and `mtools` are used only to generate regular-file test fixtures; they are not runtime dependencies. Physical card-reader enumeration remains to be verified.

Scan-button reception uses the same printer lock and the existing model-specific USB permissions. It is disabled by default and enabled explicitly in Profiles. The native transport makes a device-to-host status request with a finite transfer timeout; it does not change configuration, detach drivers or reset the device. Linux requires a writable usbfs descriptor for this input request, and opening it can resume a suspended device. Transfer timeout and cancellation do not impose a hard deadline on kernel mutex or autoresume waits. The 32/64-bit control structure is checked against compiled Linux headers. [Linux USB API](https://www.kernel.org/doc/html/latest/driver-api/usb/usb.html#the-usbfs-kernel-api), [kernel ioctl permission check](https://github.com/torvalds/linux/blob/v6.12/drivers/usb/core/devio.c)

Enabling reception, profile binding and constructed-frame tests do not establish physical button support. Hardware validation must cover real responses, repeated/held presses, connection recovery and any preparation the DCP-150C requires. Saved binding choices and activation are local device state; document backups do not activate reception on a new installation.

## Native printing

The installer checks `bh7filter --capabilities` before changing the system. It normally requires `production_ready: true`. The hardware-tested preview keeps this value false until broader physical coverage is established, so use the explicit preview option below. The installer also checks that the matching configuration tool can run.

To install the print components without creating a queue:

```sh
sudo scripts/install.sh --allow-unverified-filter --no-queue
```

The preview must identify the DCP-150C, report `native_raster: true` and support all six qualities. Placeholder builds are refused. The installer records approval for the exact executable in the root-owned `/etc/dcp150c-native/allow-unverified-filter` file. The CUPS filter checks that approval and the executable's checksum before accepting a preview build. A changed build needs explicit approval again; repeat the option when reinstalling a preview. Installing a verified build removes the preview approval. The executable continues to report its actual verification state.

Build both native executables using the project build instructions, keeping them in the same directory. Prebuilt release packages already contain both tools in `build/`. Then connect and switch on the printer. To install the preview, create its queue and share it on the local network:

```sh
sudo scripts/install.sh --allow-unverified-filter --share-lan
```

The default new queue is `DCP150C-Native`. Existing queues are preserved. Repeating the installation reuses a queue with a matching installation record and exact device URI, preserving its saved print options. Choose another name if a queue with the requested name belongs to a different setup. If an enabled legacy queue addresses the same USB printer, disable that queue before configuring the native one so every print job uses the shared device lock:

```sh
lpstat -p -v
sudo cupsdisable <old-queue-name>
sudo scripts/install.sh --allow-unverified-filter --share-lan
```

For an explicit USB URI or a different queue name:

```sh
lpinfo -v
sudo scripts/install.sh --allow-unverified-filter --queue DCP150C-Native --device-uri 'usb://Brother/DCP-150C?serial=YOUR_DEVICE_SERIAL'
```

`--share-lan` enables CUPS printer sharing and shares this queue. It does not enable unrestricted remote administration. A new queue starts unshared when this option is omitted; an existing owned queue keeps its sharing preference. The installer does not select a system-wide default printer or send a physical test page.

Other options:

- `--filter FILE`: use a different native executable, with `brprintconf_dcp150c` beside it.
- `--allow-unverified-filter`: explicitly approve the exact functional preview executable while broader hardware validation continues.
- `--no-queue`: install print components without registering a queue.
- `--no-start`: install without enabling or starting the dashboard.
- `--staging-root /absolute/path`: copy installation files into a packaging tree without changing users, services, or CUPS queues.

## Configuration and transport

`/etc/dcp150c-native/default.conf` holds default `Key=Value` printer settings. Reinstallation preserves this file and existing dashboard settings. An explicit `--queue NAME` updates the dashboard's queue selection in `queue.env`.

The full installation provides the `dcp150c-config` command:

```sh
dcp150c-config --get
sudo dcp150c-config --media Plain --resolution Fine
dcp150c-config --help
```

The configuration tool preserves comments, unknown keys and unrelated sections. An existing command named `/usr/bin/dcp150c-config` that belongs to another installation is preserved, and the installer stops before copying files.

CUPS creates a private configuration for every job; concurrent jobs do not rewrite global defaults. Quality determines the input raster resolution:

| Quality | Input resolution | Supported paper material |
| --- | --- | --- |
| Fast Draft (`Draft`) | 150 dpi | Plain |
| Fast Normal (`Fast`) | 300 dpi | Plain |
| Normal | 600 dpi | Plain, transparencies |
| Fine | 600 dpi | All supported materials |
| Photo | 600 dpi | Inkjet, Brother glossy, other glossy |
| Highest | 600 dpi | Brother glossy, other glossy |

These material combinations follow the original printer options. The PPD's `PrintQuality` setting offers all six qualities, and the CUPS filter rejects unsupported combinations or a separately selected input resolution that does not match the quality. Existing job options using `Resolution=Draft`, `Resolution=Fast` or `Resolution=Normal` remain accepted. Choose the material and quality together, for example:

```sh
lp -d DCP150C-Native -o Media=BrotherGlossy -o PrintQuality=Photo picture.pdf
```

The installer copies `calibration.dat`, the five required `brio06*.bcm` profiles and `paperinf` into `/usr/share/dcp150c-native`. The print filter receives that location through `DCP150C_DATA_DIR`. The directory also contains `BROTHER-LICENSE.txt`, the project license and the musl/GCC runtime notices in `LICENSES/`; keep these notices with redistributed installations.

The `dcp150c-filter` accepts PDF or PostScript, renders P6 RGB pages with Ghostscript, and streams them into the native filter. The custom `dcp150c:` CUPS backend delegates USB transport to the system CUPS USB backend while holding `/run/lock/dcp150c-device.lock`. The dashboard skips USB access whenever the lock or CUPS job state indicates printing. Other software that accesses the printer directly must follow the same lock convention.

Logs are available with:

```sh
journalctl -u dcp150c-dashboard
journalctl -u cups
```

## Continuous builds

The GitHub Actions workflow in `.github/workflows/build.yml` runs on pushes and pull requests. It checks native and ARMv6 builds, processor compatibility and a complete color page, then creates downloadable source-and-binary packages.

To create the same source-and-binary archive locally from a committed checkout:

```sh
scripts/package-armv6.sh
```

The package and its SHA-256 checksum are written to `build/releases/`.

## Removal

```sh
sudo scripts/uninstall.sh
```

This removes the software and queues recorded by the installer, provided each queue still has its recorded device URI. A queue whose URI changed, or an older queue without a complete ownership record, is preserved. The `dcp150c-config` command is removed only when its symbolic link still points to this installation.

Settings are preserved by default. Add `--purge-config` to remove them. The service account and lock inode are retained to avoid recycling ownership or a lock held by an older process. For a staged installation, use the same `--staging-root /absolute/path` when removing its files.
