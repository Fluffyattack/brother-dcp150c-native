#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the optional native DCP-150C SANE backend from pinned source archives.

The older brscan2 USB backend is deliberately kept separate from the decoder
project's newer-model backend. A series number alone does not identify the USB
protocol. The output contains source and licence notices alongside its libraries.
This command never enumerates or communicates with scanners.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


SOURCE_ARCHIVES = {
    "backend": {
        "url": "https://codeload.github.com/simonfojtu/brscan2/tar.gz/cd10b3cd46db375705056bc5b2aadd84bc8867e9",
        "revision": "cd10b3cd46db375705056bc5b2aadd84bc8867e9",
        "sha256": "81f79cb42fcced4579cb53dd19f7d6b2461a7ca30b1864fc7fb415378743445e",
        "filename": "brscan2-source.tar.gz",
        "maximum_bytes": 8 * 1024 * 1024,
    },
    "decoder": {
        "url": "https://codeload.github.com/dmikushin/brscan/tar.gz/51a719b7df0d31e3371868e0bb7ad0847dd2b89a",
        "revision": "51a719b7df0d31e3371868e0bb7ad0847dd2b89a",
        "sha256": "0e68af7656bd6184cffb595b39dbcfc8d072d3bc36355a85349608c792c7a0c3",
        "filename": "brscan-open.tar.gz",
        "maximum_bytes": 64 * 1024 * 1024,
    },
}


def source_file_checksum(path: Path) -> str:
    """Hash source archives in bounded chunks, including on Python 3.10."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_source_archive(cache: Path, description: dict) -> Path:
    archive = cache / description["filename"]
    if not archive.exists():
        print("Downloading pinned scanner source:", description["filename"], flush=True)
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            temporary = Path(output.name)
            try:
                request = urllib.request.Request(description["url"], headers={"User-Agent": "dcp150c-native-build"})
                with urllib.request.urlopen(request, timeout=90) as response:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > description["maximum_bytes"]:
                            raise ValueError("Scanner source archive exceeds the download limit")
                        output.write(chunk)
                output.close()
                if source_file_checksum(temporary) != description["sha256"]:
                    raise ValueError("Scanner source archive checksum did not match its pinned revision")
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
    if source_file_checksum(archive) != description["sha256"]:
        raise ValueError("Cached scanner source archive checksum did not match")
    return archive


def extract_source_files(archive: Path, destination: Path, kind: str) -> None:
    """Select regular source files only; exclude shipped binaries and captures."""
    with tarfile.open(archive, "r:gz") as source:
        count = 0
        total = 0
        for member in source:
            path = PurePosixPath(member.name)
            if not member.isfile() or len(path.parts) < 2 or ".." in path.parts or path.is_absolute():
                continue
            relative = PurePosixPath(*path.parts[1:])
            allowed = False
            if kind == "backend":
                allowed = (str(relative).startswith(("brscan/backend_src/", "brscan/include/", "brscan/sanei/",
                                                       "brscan/libbrscandec2/", "brscan/libbrcolm2/"))
                           and relative.suffix in (".c", ".h"))
                allowed |= str(relative) in ("Copying", "brscan/copying", "brscan/copying.lib", "brscan/copying.brother")
            else:
                allowed = (str(relative).startswith(("libbrscandec/", "libbrcolm/", "libsane-brother/"))
                           and relative.suffix in (".c", ".h"))
                allowed |= str(relative) in ("Copying", "copying.lib", "copying.brother")
            if not allowed:
                continue
            count += 1
            total += member.size
            if count > 500 or member.size > 4 * 1024 * 1024 or total > 16 * 1024 * 1024:
                raise ValueError("Selected scanner source exceeds extraction limits")
            output = destination.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as input_file, output.open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file)


def prepare_backend_sources(source: Path) -> None:
    # The configuration parser includes curses but uses no terminal functions.
    # Removing the unused include avoids an unrelated terminal-development package.
    configuration_parser = source / "backend_src/brother_advini.c"
    configuration_source = configuration_parser.read_bytes()
    if b"#include <curses.h>" not in configuration_source:
        raise ValueError("The pinned configuration parser no longer matches")
    configuration_parser.write_bytes(configuration_source.replace(b"#include <curses.h>\n", b""))
    runtime_directory = b"/usr/share/dcp150c-native/scanner/"
    for relative in ("backend_src/brother.h", "backend_src/brother_log.c", "backend_src/brother_advini.h"):
        path = source / relative
        original = path.read_bytes()
        if b"/usr/local/Brother/sane/" not in original:
            raise ValueError("The pinned backend no longer has its expected data path")
        path.write_bytes(original.replace(b"/usr/local/Brother/sane/", runtime_directory))
    # Keep decoder loading private to this project; do not replace system libraries.
    for relative, filename in (("backend_src/brother_scanner.c", b"libbrscandec2.so"),
                               ("backend_src/brother_cmatch.c", b"libbrcolm2.so")):
        path = source / relative
        original = path.read_bytes()
        expected = b'"' + filename + b'"'
        if expected not in original:
            raise ValueError("The pinned backend no longer has its expected library name")
        path.write_bytes(original.replace(expected, b'"/usr/lib/dcp150c-native/scanner/' + filename + b'.1"'))


def build_scanner(output_directory: Path, archive_cache: Path, compiler: str) -> None:
    if output_directory.exists() and any(output_directory.iterdir()):
        raise ValueError("Choose an empty output directory; existing scanner builds are preserved")
    output_directory.mkdir(parents=True, exist_ok=True)
    archive_cache.mkdir(parents=True, exist_ok=True)
    sources = output_directory / "sources"
    for kind, description in SOURCE_ARCHIVES.items():
        extract_source_files(fetch_source_archive(archive_cache, description), sources / kind, kind)
    backend = sources / "backend/brscan"
    decoder = sources / "decoder"
    prepare_backend_sources(backend)
    libraries = output_directory / "lib"
    libraries.mkdir()
    # The original USB parser tests char headers for negative status values.
    # ARM defaults to unsigned char; keep the original signed-header semantics.
    common = [compiler, "-O1", "-fno-strict-aliasing", "-fsigned-char", "-fPIC", "-shared", "-Wno-incompatible-pointer-types",
              "-Wno-int-conversion", "-Wno-pointer-sign", "-Wno-implicit-function-declaration"]
    commands = [
        common + [str(decoder / "libbrscandec" / name) for name in
                  ("brother_bugchk.c", "brother_scandec.c", "brother_chreso.c")]
        + ["-lm", "-Wl,-soname,libbrscandec2.so.1", "-o", str(libraries / "libbrscandec2.so.1")],
        common + [str(decoder / "libbrcolm/brcolm.c"), "-lm", "-Wl,-soname,libbrcolm2.so.1",
                  "-o", str(libraries / "libbrcolm2.so.1")],
        common + ["-fcommon", "-D_GNU_SOURCE", "-DBACKEND_NAME=brother2", "-DBRSANESUFFIX=2", "-DV_MAJOR=1", "-DV_MINOR=0"]
        + ["-I" + str(backend / path) for path in ("include", "libbrscandec2", "libbrcolm2", "backend_src")]
        + [str(backend / "backend_src/brother2.c"), str(backend / "backend_src/sane_strstatus.c")]
        + [str(backend / "sanei" / name) for name in ("sanei_config.c", "sanei_constrain_value.c", "sanei_init_debug.c")]
        + ["-lusb", "-lm", "-ldl", "-lpthread", "-Wl,-soname,libsane-brother2.so.1",
           "-o", str(libraries / "libsane-brother2.so.1")],
    ]
    for index, arguments in enumerate(commands):
        print("Building scanner component", index + 1, "of", len(commands), flush=True)
        with (output_directory / ("build-component-" + str(index + 1) + ".log")).open("wb") as diagnostics:
            subprocess.run(arguments, stdout=diagnostics, stderr=subprocess.STDOUT, check=True, timeout=600)
    data = output_directory / "data"
    data.mkdir()
    (data / "Brsane2.ini").write_text(
        '[Support Model]\n0x01cf,10,2,"DCP-150C"\n\n[ModelTypeName]\n2=DCP Scanner\n\n'
        '[Driver]\nscanfast24=0\nNoUseCM=1\ncompression=1\nInqueue=32000\nLogFile=0\n', encoding="ascii")
    (data / "brsanenetdevice2.cfg").write_text("", encoding="ascii")
    # This initial backend uses device color data directly; no vendor LUT assets.
    # Optional color processing is built and isolated but not presented as calibrated.
    manifest = {"model": "Brother DCP-150C", "format": 1, "hardware_verified": False,
                "protocol": "brscan2", "color_correction": "device output; no additional vendor correction",
                "source_archives": SOURCE_ARCHIVES, "compiler": compiler,
                "compiler_flags": common[1:],
                "libraries": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sorted(libraries.iterdir())}}
    (output_directory / "build-information.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Native scanner build is ready:", output_directory, flush=True)
    print("No scanner was contacted. Confirm a real scan before reporting hardware support.", flush=True)


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=project / "build/scanner")
    parser.add_argument("--archive-cache", type=Path, default=project / "build/scanner-source-cache")
    parser.add_argument("--compiler", default=os.environ.get("CC", "cc"))
    arguments = parser.parse_args()
    try:
        build_scanner(arguments.output_directory.resolve(), arguments.archive_cache.resolve(), arguments.compiler)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, "Scanner build failed: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
