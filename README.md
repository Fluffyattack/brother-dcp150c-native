# Brother DCP-150C Native

Native Linux printing and printer status tools for the Brother DCP-150C on Raspberry Pi.

**Development in progress. This is not yet a working replacement for the Brother printer driver.**

The project aims to replace the original x86-only raster filter with portable C code for ARM, integrate with CUPS, and provide a small local dashboard for printer status and ink levels where the hardware reports them.

## AI disclosure

This project is being developed with substantial assistance from **Claude and OpenAI Codex**. AI assistance does not establish correctness: functionality must be checked against the original driver and real hardware before being described as supported.

## Current status

- Original-driver analysis and an initial ARM prototype exist locally.
- The prototype parses input but does not implement the raster payload yet.
- Native printing, CUPS installation, USB status queries, and the dashboard are not released yet.
- No performance or ink-percentage claims have been verified.

Only product source code, installation assets, and user documentation belong in this repository. Private session history, credentials, decompilations, analysis databases, test images, and captured print jobs are excluded.

## Intended platform

Raspberry Pi OS on Raspberry Pi Zero and Zero 2 W, with the DCP-150C connected by USB. ARM64 support is also planned. This is an independent project and is not affiliated with Brother Industries.

## License

Original project code is licensed under GPL-2.0-or-later; see [LICENSE](LICENSE). Any future third-party component must retain its own license and attribution.
