# Printer calibration data

These runtime files supply the driver's color conversion, printed ink limits, dithering and error-diffusion tables. Install this directory together with the matching software release.

The calibration data is copyrighted by Brother Industries, Ltd. Its separate license is in [BROTHER-LICENSE.txt](BROTHER-LICENSE.txt); that license permits redistribution and modification. The project's original program code uses the license in the repository root.

- `calibration.dat` contains named built-in color cubes, prepared correction curves, dithering patterns and interpolation-rounding data.
- `brio06aa.bcm` contains the Inkjet Paper color profiles.
- `brio06ab.bcm` contains the Glossy Paper color profiles.
- `brio06ac.bcm` contains the Brother Glossy Paper color profiles.
- `brio06af.bcm` contains the Transparencies color profiles.
- `brio06ag.bcm` contains the Slow Drying color profiles.

The driver selects the matching profile automatically. If the original profile family has no matching entry, it uses the corresponding built-in color profile. Correction curves are prepared using the original arithmetic so every supported Pi uses identical integer tables.

## Calibration pack format

The pack starts with `BH7CAL1` and a terminating NUL byte, then a little-endian 32-bit format version and entry count. Each entry contains a 64-byte NUL-terminated name, a little-endian 32-bit payload length, its payload, and zero padding to the next four-byte boundary.

Entry names describe their role: `cube/`, `curves/`, `rows/` or `color/`. Words in payloads are little-endian. A prepared curve entry contains the 2,304-word source secondary profile, four 1,024-word tone curves, four 1,024-word signed black-correction curves, and a 256-word direct-black curve. The loader checks these against the selected Brother profile before use.
