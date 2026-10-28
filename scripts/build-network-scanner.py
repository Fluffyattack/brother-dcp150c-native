#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the optional eSCL server from verified AirSane source, without hardware.

The resulting server includes this project's shared device lock. Upstream's
installer and service are never run. Sources and build instructions accompany
the binary so the modified GPL-licensed program remains reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import runpy
import shutil
import subprocess
import tarfile


SOURCE = {
    "url": "https://codeload.github.com/SimulPiscator/AirSane/tar.gz/ac117f185e06afc7c3557f9839a42d8fd9fd18fa",
    "revision": "ac117f185e06afc7c3557f9839a42d8fd9fd18fa",
    "version": "0.4.15",
    "sha256": "53e5f168379893495efbb31284b97352ae6a7ff5662c405a717232c874a7ff78",
    "filename": "airsane-source.tar.gz",
    "maximum_bytes": 4 * 1024 * 1024,
}


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def extract_sources(archive: Path, destination: Path) -> None:
    """Extract bounded ordinary files; never create archive links or devices."""
    count = total = 0
    with tarfile.open(archive, "r:gz") as source:
        for entry in source:
            name = PurePosixPath(entry.name)
            if name.is_absolute() or ".." in name.parts or entry.issym() or entry.islnk():
                raise ValueError("The network scanner source archive contains an unsafe path")
            if entry.isdir():
                continue
            if not entry.isfile() or len(name.parts) < 2:
                raise ValueError("The network scanner source archive contains an unexpected entry")
            count += 1
            total += entry.size
            if count > 1000 or entry.size > 2 * 1024 * 1024 or total > 16 * 1024 * 1024:
                raise ValueError("The network scanner source archive exceeds its extraction limit")
            target = destination.joinpath(*name.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(entry) as input_file, target.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file)


def build_network_scanner(project: Path, output: Path, cache: Path, jobs: int) -> None:
    if not 1 <= jobs <= 2:
        raise ValueError("Choose one or two compiler processes for this Raspberry Pi build")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose an empty build directory; previous builds are preserved")
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    source_helpers = runpy.run_path(str(project / "scripts/build-scanner.py"))
    archive = source_helpers["fetch_source_archive"](cache, SOURCE)
    source_directory = output / "sources/AirSane"
    extract_sources(archive, source_directory)
    patch = project / "patches/airsane-shared-device-access.patch"
    helper_directory = source_directory / "network_scanner"
    helper_directory.mkdir()
    for name in ("device_access.h", "device_access.cpp"):
        shutil.copyfile(project / "src/network_scanner" / name, helper_directory / name)
    # Archive builds must not accidentally inherit the enclosing project's Git
    # version. This file is retained with the complete modified build sources.
    (source_directory / "version.cmake").write_text(
        'file(WRITE "${FILE}" "const char* GIT_BRANCH=\\"dcp150c\\";\\n'
        'const char* GIT_COMMIT_HASH=\\"' + SOURCE["revision"] + '\\";\\n'
        'const char* GIT_REVISION_NUMBER=\\"0.4.15+dcp150c\\";\\n'
        'const char* BUILD_TIME_STAMP=\\"2026-09-26T12:38:12Z\\";\\n")\n', encoding="ascii")
    commands = [
        ["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(patch)],
        ["cmake", "-S", str(source_directory), "-B", str(output / "objects"),
         "-DCMAKE_BUILD_TYPE=MinSizeRel"],
        ["cmake", "--build", str(output / "objects"), "--parallel", str(jobs)],
    ]
    for index, command in enumerate(commands):
        print("Building network scanner component", index + 1, "of", len(commands), flush=True)
        with (output / f"build-step-{index + 1}.log").open("wb") as diagnostics:
            subprocess.run(command, cwd=source_directory, check=True, timeout=1800,
                           stdout=diagnostics, stderr=subprocess.STDOUT)
    executable = output / "bin/airsaned"
    executable.parent.mkdir()
    shutil.copyfile(output / "objects/airsaned", executable)
    executable.chmod(0o755)
    shutil.copyfile(patch, output / "shared-device-access.patch")
    information = {"format": 1, "model": "Brother DCP-150C", "protocol": "eSCL",
                   "source": SOURCE, "license": "GPL-3.0-or-later", "hardware_verified": False,
                   "architecture": platform.machine(), "binary_sha256": checksum(executable),
                   "patch_sha256": checksum(patch), "shared_device_access": True,
                   "helper_sha256": {name: checksum(helper_directory / name)
                                     for name in ("device_access.h", "device_access.cpp")}}
    (output / "build-information.json").write_text(json.dumps(information, indent=2) + "\n")
    print("Network scanner build ready:", output, flush=True)
    print("No scanner was contacted and no network service was enabled.", flush=True)


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=project / "build/network-scanner")
    parser.add_argument("--archive-cache", type=Path, default=project / "build/network-scanner-source-cache")
    parser.add_argument("--jobs", type=int, default=1)
    arguments = parser.parse_args()
    try:
        build_network_scanner(project, arguments.output_directory.resolve(), arguments.archive_cache.resolve(), arguments.jobs)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, "Network scanner build failed: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
