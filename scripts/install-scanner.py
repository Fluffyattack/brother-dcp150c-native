#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Install an optional native scanner build without probing or starting hardware."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sysconfig


LIBRARY_NAMES = ("libbrscandec2.so.1", "libbrcolm2.so.1", "libsane-brother2.so.1")


def refuse_symbolic_link_destinations(path: Path, root: Path) -> None:
    """Preflight the entire destination chain before making any changes."""
    current = path
    while current != root and current != current.parent:
        if current.is_symlink():
            raise ValueError("Scanner installation destination contains a symbolic link: " + str(current))
        current = current.parent


def validate_native_build(build: Path) -> dict:
    information = json.loads((build / "build-information.json").read_text())
    if information.get("format") != 1 or information.get("model") != "Brother DCP-150C" or information.get("protocol") != "brscan2":
        raise ValueError("This is not a recognized DCP-150C native scanner build")
    running_header = Path("/proc/self/exe").read_bytes()[:20]
    for name in LIBRARY_NAMES:
        library = build / "lib" / name
        if library.is_symlink() or not library.is_file():
            raise ValueError("Scanner library must be a regular file: " + name)
        content = library.read_bytes()
        if hashlib.sha256(content).hexdigest() != information.get("libraries", {}).get(name):
            raise ValueError("Scanner library checksum mismatch: " + name)
        if not content.startswith(b"\x7fELF") or (content[4:6], content[18:20]) != (running_header[4:6], running_header[18:20]):
            raise ValueError("Scanner library was built for a different processor: " + name)
        # Load and resolve without calling sane_init, get_devices, open or start.
        loaded = ctypes.CDLL(str(library.resolve()))
        symbols = ("sane_brother2_init", "sane_brother2_start", "sane_brother2_read", "sane_brother2_cancel") if name.startswith("libsane") else ()
        for symbol in symbols:
            if not getattr(loaded, symbol, None):
                raise ValueError("Scanner backend is missing a required SANE entry point")
    for relative in ("sources/backend/Copying", "sources/decoder/Copying", "data/Brsane2.ini"):
        if not (build / relative).is_file():
            raise ValueError("Scanner source or license notice is missing: " + relative)
    if '0x01cf,10,2,"DCP-150C"' not in (build / "data/Brsane2.ini").read_text():
        raise ValueError("The native scanner model registration is missing")
    return information


def install_scanner(build: Path, staging_root: Path | None) -> None:
    information = validate_native_build(build)
    root = staging_root or Path("/")
    if staging_root is None and os.geteuid() != 0:
        raise ValueError("Run the scanner installer with sudo")
    if staging_root is not None and (not staging_root.is_absolute() or staging_root == Path("/")):
        raise ValueError("The staging directory must be an absolute path other than /")
    multiarch = sysconfig.get_config_var("MULTIARCH")
    if not multiarch or "/" in multiarch:
        raise ValueError("Cannot determine this system's native SANE library directory")
    backend_path = root / "usr/lib" / multiarch / "sane/libsane-brother2.so.1"
    backend_target = "/usr/lib/dcp150c-native/scanner/libsane-brother2.so.1"
    registration_path = root / "etc/sane.d/dll.d/dcp150c-native"
    record_path = root / "var/lib/dcp150c-native/scanner-installation.json"
    # Never replace another installed driver or an unrelated SANE registration.
    if backend_path.is_symlink():
        if os.readlink(backend_path) != backend_target:
            raise ValueError("A different Brother scanner backend is already installed; it was preserved")
    elif backend_path.exists():
        raise ValueError("A different Brother scanner backend is already installed; it was preserved")
    if registration_path.is_symlink() or (registration_path.exists() and registration_path.read_text() != "brother2\n"):
        raise ValueError("The scanner registration already belongs to another installation; it was preserved")
    if staging_root is not None:
        for relative in ("usr/lib", "usr/share/dcp150c-native", "etc/sane.d", "var/lib"):
            if not (root / relative).resolve().is_relative_to(root.resolve()):
                raise ValueError("The staging directory redirects outside its own tree")
    library_directory = root / "usr/lib/dcp150c-native/scanner"
    data_directory = root / "usr/share/dcp150c-native/scanner"
    copies = [(build / "lib" / name, library_directory / name) for name in LIBRARY_NAMES]
    for relative in ("data", "sources"):
        source_root = build / relative
        target_root = data_directory if relative == "data" else data_directory / "sources"
        for source in source_root.rglob("*"):
            if source.is_symlink():
                raise ValueError("Scanner source tree must not contain symbolic links")
            if source.is_file():
                copies.append((source, target_root / source.relative_to(source_root)))
    copies.append((build / "build-information.json", data_directory / "build-information.json"))
    for destination in [target for _, target in copies] + [registration_path, record_path, backend_path.parent]:
        refuse_symbolic_link_destinations(destination, root)
    for directory in (library_directory, data_directory, backend_path.parent, registration_path.parent, record_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644)
    if not backend_path.is_symlink():
        backend_path.symlink_to(backend_target)
    registration_path.write_text("brother2\n", encoding="ascii")
    registration_path.chmod(0o644)
    record = {"format": 1, "backend_path": str(backend_path.relative_to(root)),
              "backend_target": backend_target, "registration": "brother2\n",
              "libraries": information["libraries"], "hardware_verified": False}
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="ascii")
    record_path.chmod(0o644)
    print("Native DCP-150C scanner software installed; no scanner was contacted.")
    print("Use the Control Center's scanner connection check before the first requested scan.")


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-directory", type=Path, default=project / "build/scanner")
    parser.add_argument("--staging-root", type=Path)
    arguments = parser.parse_args()
    try:
        install_scanner(arguments.build_directory.resolve(), arguments.staging_root)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, "Scanner installation failed: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
