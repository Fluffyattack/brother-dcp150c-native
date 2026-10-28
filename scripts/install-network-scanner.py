#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Install the optional network scanner, leaving its listener switched off."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil


def create_readable_directories(directory: Path, root: Path) -> None:
    """Make newly installed public directories traversable despite a private umask.

    Existing parent permissions and the staging root are preserved. Individual
    files still receive their own explicit executable or read-only modes.
    """
    missing = []
    current = directory
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        created.chmod(0o755)


def install_network_scanner(build: Path, staging_root: Path | None) -> None:
    information = json.loads((build / "build-information.json").read_text())
    if (information.get("format") != 1 or information.get("model") != "Brother DCP-150C"
            or information.get("protocol") != "eSCL" or information.get("shared_device_access") is not True
            or information.get("source", {}).get("revision") != "ac117f185e06afc7c3557f9839a42d8fd9fd18fa"):
        raise ValueError("This is not a recognized network scanner build with shared device access")
    executable = build / "bin/airsaned"
    if executable.is_symlink() or not executable.is_file():
        raise ValueError("The network scanner executable must be a regular file")
    content = executable.read_bytes()
    native_header = Path("/proc/self/exe").read_bytes()[:20]
    if (not content.startswith(b"\x7fELF") or (content[4:6], content[18:20]) != (native_header[4:6], native_header[18:20])
            or hashlib.sha256(content).hexdigest() != information.get("binary_sha256")):
        raise ValueError("The network scanner checksum or processor does not match")
    sources = build / "sources/AirSane"
    if not (sources / "LICENSE").is_file() or not (sources / "network_scanner/device_access.cpp").is_file():
        raise ValueError("The complete modified network scanner source and license are required")
    patch = build / "shared-device-access.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != information.get("patch_sha256"):
        raise ValueError("The network scanner source patch checksum does not match")
    for name in ("device_access.h", "device_access.cpp"):
        helper = sources / "network_scanner" / name
        if hashlib.sha256(helper.read_bytes()).hexdigest() != information.get("helper_sha256", {}).get(name):
            raise ValueError("The network scanner source helper checksum does not match")
    if staging_root is None and os.geteuid() != 0:
        raise ValueError("Run the network scanner installer with sudo")
    root = staging_root or Path("/")
    if not root.is_absolute() or (staging_root is not None and root == Path("/")):
        raise ValueError("Choose an absolute staging directory other than /")
    helper = runpy.run_path(str(Path(__file__).with_name("install-scanner.py")))
    refuse_links = helper["refuse_symbolic_link_destinations"]
    data = root / "usr/share/dcp150c-native/network-scanner"
    copies = [(executable, root / "usr/lib/dcp150c-native/network-scanner/airsaned"),
              (build / "build-information.json", data / "build-information.json"),
              (build / "shared-device-access.patch", data / "shared-device-access.patch")]
    for source in sources.rglob("*"):
        if source.is_symlink():
            raise ValueError("The network scanner source tree contains a symbolic link")
        if source.is_file():
            copies.append((source, data / "sources" / source.relative_to(sources)))
    for source, target in copies:
        if source.is_symlink():
            raise ValueError("The network scanner build contains a symbolic link")
        refuse_links(target, root)
    # The isolated backend list prevents this service from opening unrelated
    # physical or network scanners. It is never added to the system dll.conf.
    configuration = root / "etc/dcp150c-native/network-scanner/sane/dll.conf"
    refuse_links(configuration, root)
    if configuration.exists() and configuration.read_text() != "brother2\n":
        raise ValueError("A different network scanner configuration exists; it was preserved")
    for source, target in copies:
        create_readable_directories(target.parent, root)
        temporary = target.with_name(target.name + ".installing")
        refuse_links(temporary, root)
        shutil.copyfile(source, temporary)
        temporary.chmod(0o755 if source == executable else 0o644)
        temporary.replace(target)
    create_readable_directories(configuration.parent, root)
    configuration.write_text("brother2\n", encoding="ascii")
    configuration.chmod(0o644)
    print("Optional network scanner installed. Enable the LAN share explicitly in the Control Center.")
    print("No scanner was contacted, listener started, or firewall changed.")


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-directory", type=Path, default=project / "build/network-scanner")
    parser.add_argument("--staging-root", type=Path)
    arguments = parser.parse_args()
    try:
        install_network_scanner(arguments.build_directory.resolve(), arguments.staging_root)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, "Network scanner installation failed: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
