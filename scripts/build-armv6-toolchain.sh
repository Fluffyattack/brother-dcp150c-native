#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Build a small ARMv6-compatible static C runtime using the installed cross GCC.
set -euo pipefail

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
toolchain_dir=$project_dir/build/toolchain-armv6
source_dir=$project_dir/build/musl-1.2.6
archive=$project_dir/build/musl-1.2.6.tar.gz
expected=d585fd3b613c66151fc3249e8ed44f77020cb5e6c1e635a616d3f9f82460512a
runtime_archive=$project_dir/build/raspbian-libgcc-12-armv6.deb
runtime_directory=$project_dir/build/runtime-gcc-armv6
runtime_libraries=$runtime_directory/usr/lib/gcc/arm-linux-gnueabihf/12
runtime_checksum=4a567fd9afee69ad9ffab4598e78c85ce66a192afddba3a0d17b929d95ca1aa8

command -v arm-linux-gnueabihf-gcc >/dev/null || {
    echo 'Install gcc-arm-linux-gnueabihf before building ARMv6 software.' >&2
    exit 1
}
if [ -x "$toolchain_dir/bin/musl-gcc" ] && [ -f "$toolchain_dir/lib/libc.a" ] \
    && [ -f "$runtime_libraries/libgcc.a" ] \
    && [ -f "$toolchain_dir/armv6-runtime-complete" ]; then
    exit 0
fi
mkdir -p "$project_dir/build"
python3 - "$archive" "$expected" "$runtime_archive" "$runtime_checksum" <<'PY'
import hashlib
from pathlib import Path
import sys
import urllib.request

downloads = [
    (sys.argv[1], sys.argv[2], 'https://musl.libc.org/releases/musl-1.2.6.tar.gz'),
    (sys.argv[3], sys.argv[4], 'https://archive.raspbian.org/raspbian/pool/main/g/gcc-12/'
     'libgcc-12-dev_12.2.0-14%2Brpi1%2Bdeb12u1_armhf.deb'),
]
for filename, expected, url in downloads:
    destination = Path(filename)
    if not destination.exists():
        temporary = destination.with_suffix('.download')
        try:
            with urllib.request.urlopen(url, timeout=30) as source:
                with temporary.open('wb') as output:
                    total = 0
                    while chunk := source.read(65536):
                        total += len(chunk)
                        if total > 8 * 1024 * 1024:
                            raise SystemExit('Runtime download exceeded its size limit')
                        output.write(chunk)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
                raise SystemExit('Runtime download checksum mismatch')
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
        raise SystemExit('Cached runtime download checksum mismatch')
PY
if [ ! -f "$runtime_libraries/libgcc.a" ]; then
    dpkg-deb -x "$runtime_archive" "$runtime_directory"
fi
if [ ! -f "$toolchain_dir/lib/libc.a" ]; then
if [ ! -x "$source_dir/configure" ]; then
    tar -xzf "$archive" -C "$project_dir/build"
fi
cd "$source_dir"
./configure --target=arm-linux-musleabihf --prefix="$toolchain_dir" \
    --disable-shared --enable-wrapper=gcc \
    CC=arm-linux-gnueabihf-gcc \
    AR=arm-linux-gnueabihf-ar RANLIB=arm-linux-gnueabihf-ranlib \
    CFLAGS='-Os -marm -march=armv6 -mfpu=vfp -mfloat-abi=hard'
make -j "${DCP150C_BUILD_JOBS:-2}"
make install
install -m 0644 COPYRIGHT "$toolchain_dir/COPYRIGHT"
fi

# The compiler's startup and arithmetic objects must also support ARMv6.
python3 - "$toolchain_dir" "$runtime_libraries" <<'PY'
from pathlib import Path
import shlex
import sys

toolchain = Path(sys.argv[1])
runtime = Path(sys.argv[2])
wrapper = toolchain / 'bin/musl-gcc'
script = '#!/bin/sh\nexec "${REALGCC:-arm-linux-gnueabihf-gcc}" -B '
script += shlex.quote(str(runtime) + '/') + ' "$@" -specs '
script += shlex.quote(str(toolchain / 'lib/musl-gcc.specs')) + '\n'
wrapper.write_text(script, encoding='utf-8')
wrapper.chmod(0o755)
(toolchain / 'armv6-runtime-complete').write_text('musl 1.2.6; Raspbian GCC 12 ARMv6 runtime\n')
PY
