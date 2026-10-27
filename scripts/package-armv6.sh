#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Package the committed software and matching static Pi binaries together.
set -euo pipefail

project_directory=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_directory"
if ! git diff --quiet HEAD --; then
    echo 'Commit the software changes before packaging a release.' >&2
    exit 1
fi

revision=$(git rev-parse HEAD)
version=$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)
package_name=brother-dcp150c-native-$version-armv6
output_directory=$project_directory/build/releases
mkdir -p "$project_directory/build"
temporary_directory=$(mktemp -d "$project_directory/build/release-package.XXXXXX")
trap 'rm -rf -- "$temporary_directory"' EXIT

make armv6
mkdir -p "$output_directory"
git archive --format=tar --prefix="$package_name/" HEAD | tar -xf - -C "$temporary_directory"
package_directory=$temporary_directory/$package_name
install -d -m 0755 "$package_directory/build"
install -m 0755 build/armv6/bh7filter build/armv6/brprintconf_dcp150c "$package_directory/build/"
ln -s brprintconf_dcp150c "$package_directory/build/brprintconfij2"
cat > "$package_directory/BUILD-INFORMATION.txt" <<INFORMATION
Brother DCP-150C Native: $version
Source revision: $revision
Target: ARMv6 hard-float Linux, including the original Raspberry Pi Zero.
The two programs in build/ are statically linked with musl and the GCC runtime.
Source code, runtime printer data and license notices are included.
Physical printer validation remains pending; see README.md before installing.
INFORMATION

source_date=$(git show -s --format=%ct HEAD)
archive=$output_directory/$package_name.tar.gz
tar --sort=name --mtime="@$source_date" --owner=0 --group=0 --numeric-owner \
    -czf "$archive" -C "$temporary_directory" "$package_name"
(cd "$output_directory" && sha256sum "$package_name.tar.gz" > "$package_name.tar.gz.sha256")
printf 'Release package: %s\n' "$archive"
