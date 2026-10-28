# Optional network scanner

The Control Center can supervise a separate eSCL listener for scanner programs
on the printer Pi's local network. eSCL is the network scanning protocol used by
AirScan-compatible applications. The optional component uses
[AirSane](https://github.com/SimulPiscator/AirSane), pinned to version 0.4.15,
with a source patch for this project's device access and resource limits.

The share starts **disabled**. Enabling it allows clients on the selected
interface's connected networks to request scans without the dashboard password.
Their scanner program receives and saves the image; these files are not added to
the Control Center's document library. This listener is separate from the
password-protected browser dashboard and its remote HTTPS tunnel.

Sharing settings persist locally on this Pi. Importing profiles or restoring a
document backup does not enable a network share on another installation.

## Build and install

First install the main project and its [native scanner backend](scanner-setup.md).
Build the optional network component on the Pi where it will run:

```sh
sudo apt install build-essential cmake patch libsane-dev libjpeg-dev libpng-dev libavahi-client-dev libusb-1.0-0-dev
python3 scripts/build-network-scanner.py
sudo python3 scripts/install-network-scanner.py
sudo systemctl restart dcp150c-dashboard
```

The builder verifies the upstream archive checksum, applies the local patch
without fuzzy matching and runs one compiler process at a time. It does not run
the resulting executable or enumerate a scanner. The installer adds the binary,
build manifest, modified sources and GPL license under this project's own
directories. It never runs upstream's service installer, changes firewall rules
or enables sharing. The normal printer package does not include this optional
binary.

## Enable a share

With the printer connected, open **Settings → Network scanner**. Select the
local network interface and an unused port, initially 8090. Review the local
access explanation and explicitly save the enabled share. The listener discovers
only the configured native backend and publishes this model through multicast
DNS on the selected interface. The dashboard shows the listener's cached state;
opening this card does not search for devices or acquire an image.

Compatible scanner applications can discover the share on the same network.
For a client that supports manual eSCL configuration, the first scanner's base
address is `http://<printer-pi>:8090/eSCL/`. Use the address shown for your
selected local interface and the port you saved. Authentication for the browser
dashboard does not authenticate this separate eSCL address.

Automatic USB reload is disabled to avoid replacing scanner state during an
active acquisition. After connecting or switching on the printer, use **Restart
sharing** if the client has no scanner. A network address change stops the old
listener and also requires a deliberate restart. Restarting performs device
discovery; it does not request an image. Disabling sharing stops the listener,
including a scan currently running through it.

## Coordination and limits

Printing, ink polling, browser scans and network acquisition use the same kernel
file lock. Discovery briefly takes that lock; idle listening does not. The lock
remains held until the native scanner handle has actually closed. A busy device
refuses a concurrent acquisition rather than running both protocols together.

Network scans are limited to 600 dots per inch and 40 million decoded pixels.
The server preserves fractional millimetres when applying a selected region,
accepts at most eight HTTP handlers, keeps at most sixteen job records and limits
scan tickets to 64 KiB. Job records expire after thirty inactive seconds. Inactive
socket reads and writes time out after 30 seconds. A held metadata operation is
limited to 60 seconds and a held acquisition to 180 seconds. The supervisor
terminates a stuck child, then uses a forced stop after five seconds if needed.
An uninterruptible kernel call can still delay actual process exit; the
supervisor never reports a released device before that exit.

A failed or timed-out acquisition is not retried automatically. The share stays
in its error state until a deliberate restart or settings change. Scanner
clients may impose their own time limits and cancellation behaviour.

## Verification and physical acceptance

The public checks separate child-process supervision, the native locking patch
and complete network acquisition. The latter uses the real server, a separate
`sane-airscan` client and SANE's isolated `test` backend, with multicast discovery
disabled. No physical scanner backend is loaded:

```sh
sudo apt install sane-utils sane-airscan
DCP150C_AIRSANE_TEST_BINARY="$PWD/build/network-scanner/bin/airsaned" \
    python3 -m unittest verification.test_network_scanner_integration -v
python3 -m unittest verification.test_network_scanner_service -v
```

Virtual images and protocol checks do not establish physical DCP-150C scanner
support or compatibility with every scanner application. Actual image capture,
cancellation, switching back to printing and client discovery still require
separate device acceptance. The interface continues to label this feature as a
preview while those checks remain open.

## License and removal

The modified AirSane binary and the added C++ helper are GPL-3.0-or-later.
The build directory retains its exact modified source; installation places that
source, upstream license and patch under
`/usr/share/dcp150c-native/network-scanner/`. Keep them with any redistributed
binary. Existing project Python code remains GPL-2.0-or-later.

Disable sharing before removing the optional binary. Its files are confined to
`/usr/lib/dcp150c-native/network-scanner/`,
`/usr/share/dcp150c-native/network-scanner/` and
`/etc/dcp150c-native/network-scanner/`. Remove those directories only when you no
longer need the component, then restart the dashboard. Documents, printer
configuration and the separately installed native scanner remain available.
