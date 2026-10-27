# Raspberry Pi OS installation

The installer supports Raspberry Pi OS with systemd and CUPS 2.x. The dashboard uses Python's standard library. Native printing additionally needs Ghostscript and a verified `bh7filter` built for the Pi's CPU, together with the matching `brprintconf_dcp150c` configuration tool. Printer hardware testing remains outstanding.

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

Dashboard settings go in `/etc/dcp150c-native/dashboard.env`. The installer preserves this file when it already exists. Its initial settings leave model-specific USB status disabled:

```ini
DCP150C_MODEL_STATUS=0
```

That setting stays off until hardware testing is complete. Enabling it later also requires this project's CUPS backend, no queued print jobs, an identified DCP-150C and exclusive access to the shared device lock. Queue information and any available CUPS ink markers remain accessible with the setting off.

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

## Native printing

The installer checks `bh7filter --capabilities` before changing the system. It normally requires `production_ready: true`. It also checks that the matching configuration tool can run. Development builds can use the dashboard-only installation.

To install a functional preview before hardware testing, use the explicit option below. This installs the print components without creating a queue:

```sh
sudo scripts/install.sh --allow-unverified-filter --no-queue
```

The preview must identify the DCP-150C, report `native_raster: true` and support all six qualities. Placeholder builds are refused. The installer records approval for the exact executable in the root-owned `/etc/dcp150c-native/allow-unverified-filter` file. The CUPS filter checks that approval and the executable's checksum before accepting a preview build. A changed build needs explicit approval again; repeat the option when reinstalling a preview. Installing a verified build removes the preview approval. The executable continues to report its actual verification state.

Build both native executables using the project build instructions, keeping them in the same directory. Then connect and switch on the printer:

```sh
sudo scripts/install.sh --share-lan
```

The default new queue is `DCP150C-Native`. Existing queues are preserved. Repeating the installation reuses a queue with a matching installation record and exact device URI, preserving its saved print options. Choose another name if a queue with the requested name belongs to a different setup. If an enabled legacy queue addresses the same USB printer, disable that queue before configuring the native one so every print job uses the shared device lock:

```sh
lpstat -p -v
sudo cupsdisable <old-queue-name>
sudo scripts/install.sh --share-lan
```

For an explicit USB URI or a different queue name:

```sh
lpinfo -v
sudo scripts/install.sh --queue DCP150C-Native --device-uri 'usb://Brother/DCP-150C?serial=YOUR_DEVICE_SERIAL'
```

`--share-lan` enables CUPS printer sharing and shares this queue. It does not enable unrestricted remote administration. A new queue starts unshared when this option is omitted; an existing owned queue keeps its sharing preference. The installer does not select a system-wide default printer or send a physical test page.

Other options:

- `--filter FILE`: use a different native executable, with `brprintconf_dcp150c` beside it.
- `--allow-unverified-filter`: explicitly approve a functional preview build before hardware testing.
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

`github-build-workflow.yml` is a GitHub Actions template for native and ARMv6 builds, processor compatibility checks, complete color-page comparison and downloadable binary packages. To enable it, copy it to `.github/workflows/build.yml` and commit that file using a GitHub connection with permission to update workflows. The template does not run automatically from its current location.

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
