# SPDX-License-Identifier: GPL-2.0-or-later
"""Run document tools with a deadline and cancellation for their whole process tree."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Mapping, Sequence


class CommandError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def stop_process_group(process: subprocess.Popen) -> None:
    """Reap the leader and stop descendants even when a tool starts a renderer."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A child may outlive its parent or ignore SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(arguments: Sequence[str | Path], *, cancel_event: threading.Event | None = None,
                timeout: float = 120, output_path: Path | str | None = None,
                cwd: Path | str | None = None, env: Mapping[str, str] | None = None,
                check: bool = True) -> subprocess.CompletedProcess:
    """Capture small diagnostics on disk; write binary documents directly to a file.

    No shell is involved. Callers own document size/page limits. This runner bounds
    execution time and captured diagnostics without retaining document bytes in logs.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise CommandError("job_cancelled", "The operation was cancelled.")
    command = [os.fspath(argument) for argument in arguments]
    environment = {**os.environ, "LC_ALL": "C.UTF-8", "OMP_THREAD_LIMIT": "1"}
    if env:
        environment.update(env)
    with tempfile.TemporaryFile() as standard_error, tempfile.TemporaryFile() as captured_output:
        output_file = open(output_path, "wb") if output_path is not None else captured_output
        process = None
        try:
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output_file,
                                           stderr=standard_error, cwd=cwd, env=environment,
                                           start_new_session=True, close_fds=True)
            except OSError as error:
                raise CommandError("dependency_unavailable", "A required processing tool could not start.") from error
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.05):
                    stop_process_group(process)
                    raise CommandError("job_cancelled", "The operation was cancelled.")
                if time.monotonic() >= deadline:
                    stop_process_group(process)
                    raise CommandError("processing_timeout", "The operation exceeded its time limit.")
                if (os.fstat(standard_error.fileno()).st_size > 524288
                        or (output_path is None and os.fstat(captured_output.fileno()).st_size > 524288)
                        or (output_path is not None and os.fstat(output_file.fileno()).st_size > 128 * 1024 * 1024)):
                    stop_process_group(process)
                    raise CommandError("processing_output_limit", "The processing tool returned too much diagnostic output.")
                if cancel_event is None:
                    time.sleep(0.05)
            standard_error.seek(0)
            captured_output.seek(0)
            if (os.fstat(standard_error.fileno()).st_size > 524288
                    or (output_path is None and os.fstat(captured_output.fileno()).st_size > 524288)):
                raise CommandError("processing_output_limit", "The processing tool returned too much diagnostic output.")
            result = subprocess.CompletedProcess(command, process.returncode,
                                                 captured_output.read(524288), standard_error.read(524288))
            if check and result.returncode:
                raise CommandError("processing_failed", "The processing tool could not finish the operation.")
            return result
        finally:
            if process is not None and process.poll() is None:
                stop_process_group(process)
            if output_path is not None:
                output_file.close()
