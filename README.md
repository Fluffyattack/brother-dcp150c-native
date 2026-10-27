# Brother DCP-150C Native

Native Linux printing and a local printer dashboard for the **Brother DCP-150C on Raspberry Pi**.

**Preview software: physical print validation is still pending.** The native converter has been compared with the original driver, including complete color pages. USB connection and basic printer status have been verified on a Pi Zero 2 W; no physical test page was sent. Build information continues to report `production_ready: false`. The dashboard can be installed independently.

The software includes:

- A portable C program that converts page pixels into DCP-150C printer commands, without running an x86 driver through an emulator.
- Integration with the Common UNIX Printing System (CUPS), including PDF/PostScript rendering, six quality settings and a separate printer queue.
- A small dashboard with printing activity, connection status and available ink information. It runs without Python package dependencies.
- A configuration tool with readable options and installation/removal scripts that preserve existing printer settings.

## Get started on the Pi

The [prebuilt Pi package](https://github.com/Fluffyattack/brother-dcp150c-native/releases/tag/v0.1.0-preview.1) includes static ARMv6 programs, their source code, required runtime data and installation scripts. Download and unpack it to use the programs already in `build/`; compilation is optional.

Use Raspberry Pi OS with Python 3.10 or newer, systemd and CUPS 2.x:

```sh
sudo apt update
sudo apt install git build-essential python3 cups cups-client cups-filters ghostscript avahi-daemon
git clone https://github.com/Fluffyattack/brother-dcp150c-native.git
cd brother-dcp150c-native
make
```

`make` builds the native raster converter `build/bh7filter` and configuration tool `build/brprintconf_dcp150c` for the machine doing the build. Both 32-bit ARM and ARM64 use the same source code.

To install just the dashboard and monitor an existing queue:

```sh
lpstat -p
sudo scripts/install.sh --dashboard-only --queue YOUR_EXISTING_QUEUE
```

Open `http://<pi-address>:8080/`. For a quick run without installing a service:

```sh
python3 -m dcp150c --queue YOUR_EXISTING_QUEUE
```

Unavailable ink readings appear as unknown. Status requests wait while the printer connection is in use.

## Try native printing

The default installer requires a hardware-validated filter. This preview provides an explicit option for testing the native implementation:

```sh
sudo scripts/install.sh --allow-unverified-filter
```

Connect and switch on the printer first. The installer creates the separate queue `DCP150C-Native`; it preserves existing queues. An enabled older queue for the same USB printer must be paused before using the shared connection. The installer explains which queue needs attention and sends no test page itself.

After installation, submit a document with:

```sh
lp -d DCP150C-Native -o PrintQuality=Normal document.pdf
dcp150c-config --get
```

The converter accepts complete eight-bit RGB portable pixmap (P6) pages. CUPS uses Ghostscript to create these pages from documents. Quality determines the input resolution: Draft uses 150 dots per inch, Fast uses 300, and Normal/Fine/Photo/Highest use 600. The printer options enforce the supported combinations of quality and paper material.

See [installation, configuration and removal](packaging/README.md) for network sharing, explicit USB selection, alternative build locations and troubleshooting.

## Ink and printer status

The dashboard reads connection information and printing activity from Linux and CUPS. Available CUPS ink markers may come from a previous driver reading.

Optional model-specific USB polling is implemented but **disabled by default**. The connected DCP-150C returned no cartridge counters during status-only USB queries, so this feature remains experimental. It requires this project's shared USB backend and an idle printer. When available, model-specific readings use the original monitor's level bands: **0, 10, 25, 40, 55, 70, 85 and 100 percent**. A missing response remains unknown.

Dashboard settings are kept in `/etc/dcp150c-native/dashboard.env`. Installation preserves that file. Details are in the [status configuration instructions](packaging/README.md#dashboard-only).

## Build for the original Pi Zero

On a Debian/Ubuntu Linux development host:

```sh
sudo apt install gcc-arm-linux-gnueabihf python3 make
make armv6
```

This produces static binaries in `build/armv6/`, including support for the ARM1176 processor in the original Pi Zero. The build uses checksum-verified musl 1.2.6 sources and the ARMv6 startup/arithmetic runtime from Raspbian GCC 12. Build dependencies remain inside the ignored `build/` directory.

The [automated build](.github/workflows/build.yml) executes these binaries with the ARM1176 CPU model and compares a complete color page with the host build. It also packages the programs with their source and runtime data. For installation on a Pi, select both matching tools with:

```sh
sudo scripts/install.sh --filter build/armv6/bh7filter --allow-unverified-filter
```

## Find your way around the code

| Location | Purpose |
| --- | --- |
| `src/filter.c`, `src/input.c` | Command-line options, printer settings and page dimensions |
| `src/pipeline.c` | Read pages and coordinate conversion in bounded chunks |
| `src/profile.c`, `src/calibration.c` | Load and select the printer's color calibration |
| `src/color.c`, `src/rows.c` | Convert RGB pixels into packed ink rows |
| `src/planes.c`, `src/codecs.c`, `src/device.c` | Arrange nozzles, compress rows and write printer commands |
| `src/printconfig.c` | Read and safely update configuration files |
| `data/`, `config/` | Required printer calibration, paper dimensions and defaults |
| `dcp150c/` | Status reader, HTTP server and dashboard |
| `packaging/`, `scripts/` | Print-service integration and installation |

`bh7filter` is the compatibility name of the raster converter. `Bh7` identifiers refer to this printer engine. External configuration names and protocol identifiers are retained for compatibility; project functions and messages describe their purpose.

## AI disclosure

This project was developed with substantial assistance from **Claude and OpenAI Codex**. Validation includes comparisons with the original driver, processor compatibility checks and dashboard tests. These software checks do not replace testing the physical printer.

Only project source code, required runtime data, installation files and user documentation are published here.

## Related projects

- [brlaser](https://github.com/pdewacht/brlaser): an independent native driver for Brother laser printers.
- [brother-in-arms](https://github.com/alexivkin/brother-in-arms): ARM packaging for selected Brother laser models.

This project targets the DCP-150C inkjet engine. It is independent of Brother Industries.

## Licenses

Original project code is licensed under [GPL-2.0-or-later](LICENSE). Printer calibration data is distributed under the separate [Brother software license](data/BROTHER-LICENSE.txt). Static ARMv6 builds also include musl and GCC runtime code; their notices are in [LICENSES](LICENSES/). Keep all applicable notices with redistributed software.
