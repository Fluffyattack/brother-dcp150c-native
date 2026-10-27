"""Bounded DCP-150C status parsing and bidirectional USB polling.

The model reports stepped cartridge levels rather than precise percentages.
The caller must verify the device identity, check CUPS activity and hold the
shared printer lock before using either transport function. Cartridge levels
and printer status are requested separately; this module does not enable
polling on its own.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import math
import os
import re
import select
import stat
import time
from typing import Callable


UNIVERSAL_EXIT_LANGUAGE = b"\x1b%-12345X"
COMMAND_PREFIX = UNIVERSAL_EXIT_LANGUAGE + b"@PJL\n"
COMMAND_PADDING = b""
ENABLE_STATUS_COMMANDS = (
    b"@PJL USTATUS DEVICE=ON \n",
    b"@PJL USTATUS DEVICE2=ON \n",
)
DISABLE_STATUS_COMMANDS = (
    b"@PJL USTATUS DEVICE=OFF \n",
    b"@PJL USTATUS DEVICE2=OFF \n",
)
REPORTED_PERCENTAGE_STEPS = (0, 10, 25, 40, 55, 70, 85, 100)
STATUS_SOURCE = "brother-pjl-usb"
_EMPTY_PACKET_PAUSE_SECONDS = 0.01
_CARTRIDGE_TAGS = {0xA1: "k", 0xA2: "c", 0xA3: "m", 0xA4: "y"}
_CARTRIDGE_LABELS = {"c": "Cyan", "m": "Magenta", "y": "Yellow", "k": "Black"}
_STATUS_MESSAGES = {
    40021: ("cover_open", "The printer cover is open."),
    40022: ("paper_jam", "The printer reports a paper jam."),
    41200: ("out_of_paper", "The printer reports that paper is empty."),
    40038: ("ink_empty", "The printer reports an empty ink cartridge."),
}
_STATUS_HEADER = re.compile(rb"@PJL(?:[ \t]+[A-Za-z0-9_=.-]+)*")
_STATUS_FIELD = re.compile(rb"([A-Z][A-Z0-9_]{0,31})[ \t]*=[ \t]*(.*)")
_DECIMAL_VALUE = re.compile(rb"[0-9]{1,10}")
_SIGNED_DECIMAL_VALUE = re.compile(rb"-?[0-9]{1,10}")
_HEX_BYTE = re.compile(rb"(?:0[xX])?[0-9A-Fa-f]{1,2}")


class StatusProtocolError(ValueError):
    """The printer response is incomplete, malformed or exceeds its bounds."""


class StatusTransportError(OSError):
    """A status exchange failed, including any notification-cleanup failures."""

    def __init__(self, message: str, *, cleanup_errors: tuple[str, ...] = (),
                 partial_status: dict | None = None):
        super().__init__(message)
        self.cleanup_errors = cleanup_errors
        self.partial_status = partial_status


def stepped_ink_percentage(current: int, maximum: int) -> int | None:
    """Return the original stepped display value; a zero maximum is unknown."""
    if type(current) is not int or type(maximum) is not int:
        raise ValueError("Cartridge counters must be whole numbers.")
    if not 0 <= current <= 255 or not 0 <= maximum <= 255:
        raise ValueError("Cartridge counters must each fit in one byte.")
    if maximum == 0:
        return None
    percentage = current * 100 // maximum
    if percentage == 0:
        return 0
    for upper_boundary, displayed_percentage in (
        (10, 10), (25, 25), (40, 40), (55, 55), (70, 70), (85, 85),
    ):
        if percentage < upper_boundary:
            return displayed_percentage
    return 100


def _unknown_cartridges() -> dict:
    return {
        cartridge: {
            "label": label, "percent": None, "state": "unknown",
            "source": None, "granularity": "stepped",
            "raw_current": None, "raw_maximum": None,
            "raw_percent_floor": None,
        }
        for cartridge, label in _CARTRIDGE_LABELS.items()
    }


class StatusResponseParser:
    """Parse fragmented PJL lines without retaining an unbounded device stream.

    Fields are accepted only after a complete PJL header line. A final partial
    line is discarded and reported, so a truncated cartridge record cannot
    become a fabricated level. Other complete fields remain available.
    """

    def __init__(self, maximum_response_bytes: int = 32768,
                 maximum_line_bytes: int = 512, maximum_frames: int = 32):
        if not 1 <= maximum_response_bytes <= 65536:
            raise ValueError("The response limit must be between 1 and 65536 bytes.")
        if not 16 <= maximum_line_bytes <= 512:
            raise ValueError("The line limit must be between 16 and 512 bytes.")
        if not 1 <= maximum_frames <= 32:
            raise ValueError("The frame limit must be between 1 and 32.")
        self.maximum_response_bytes = maximum_response_bytes
        self.maximum_line_bytes = maximum_line_bytes
        self.maximum_frames = maximum_frames
        self.total_bytes = 0
        self.frame_count = 0
        self._pending_line = bytearray()
        self._inside_frame = False
        self._finished = False
        self._fields_seen = 0
        self._cartridges_seen: set[str] = set()
        self._status_code: int | None = None
        self._online: bool | None = None
        self._frame_has_status_code = False
        self._completed_status_frames = 0
        self._display: str | None = None
        self._display_raw: bytes | None = None
        self._display_encoding: str | None = None
        self._character_set: str | None = None
        self._device_status: int | None = None
        self._cartridges = _unknown_cartridges()
        self._unknown_tags: list[dict] = []
        self._warnings: list[str] = []

    @property
    def has_status_fields(self) -> bool:
        return self._fields_seen != 0

    @property
    def has_complete_ink_and_status(self) -> bool:
        return len(self._cartridges_seen) == 4 and self._status_code is not None

    @property
    def has_all_cartridges(self) -> bool:
        return len(self._cartridges_seen) == 4

    @property
    def completed_status_frames(self) -> int:
        return self._completed_status_frames

    def _record_warning(self, message: str) -> None:
        if message not in self._warnings and len(self._warnings) < 16:
            self._warnings.append(message)

    def _end_current_frame(self) -> None:
        if self._inside_frame and self._frame_has_status_code:
            self._completed_status_frames += 1
        self._inside_frame = False
        self._frame_has_status_code = False

    def _remove_framing_prefix(self) -> None:
        while self._pending_line:
            if self._pending_line[0] == 0:
                del self._pending_line[0]
            elif self._pending_line[0] == 12:
                self._end_current_frame()
                del self._pending_line[0]
            elif self._pending_line.startswith(UNIVERSAL_EXIT_LANGUAGE):
                self._end_current_frame()
                del self._pending_line[:len(UNIVERSAL_EXIT_LANGUAGE)]
            else:
                return

    def feed(self, data: bytes) -> None:
        if self._finished:
            raise StatusProtocolError("The status parser has already finished.")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("Printer response data must be bytes.")
        if self.total_bytes + len(data) > self.maximum_response_bytes:
            raise StatusProtocolError("The printer response exceeded its byte limit.")
        self.total_bytes += len(data)
        self._pending_line.extend(data)
        while self._pending_line:
            self._remove_framing_prefix()
            newline_position = self._pending_line.find(b"\n")
            if newline_position < 0:
                if len(self._pending_line) > self.maximum_line_bytes:
                    raise StatusProtocolError("A printer status line exceeded its byte limit.")
                return
            if newline_position > self.maximum_line_bytes:
                raise StatusProtocolError("A printer status line exceeded its byte limit.")
            line = bytes(self._pending_line[:newline_position])
            del self._pending_line[:newline_position + 1]
            self._consume_line(line)

    def _consume_line(self, line: bytes) -> None:
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line:
            return
        if any((value < 32 and value != 9) or value == 127 for value in line):
            raise StatusProtocolError("A printer status line contained invalid control bytes.")
        if line.startswith(b"@PJL"):
            if len(line) > 128 or not _STATUS_HEADER.fullmatch(line):
                raise StatusProtocolError("The printer returned a malformed PJL header.")
            self._end_current_frame()
            self.frame_count += 1
            if self.frame_count > self.maximum_frames:
                raise StatusProtocolError("The printer returned too many status frames.")
            self._inside_frame = True
            return
        if not self._inside_frame:
            self._record_warning("Unframed printer data was ignored.")
            return
        match = _STATUS_FIELD.fullmatch(line)
        if not match:
            self._record_warning("An unrecognized printer status line was ignored.")
            return
        name, value = match.groups()
        value = value.strip(b" \t")
        if value.startswith(b'"') or value.endswith(b'"'):
            if len(value) < 2 or not (value.startswith(b'"') and value.endswith(b'"')):
                raise StatusProtocolError("A printer status value had an incomplete quoted string.")
            value = value[1:-1]
        if name == b"CODE":
            self._status_code = self._parse_decimal_status_value(value, signed=False)
            self._frame_has_status_code = True
        elif name == b"ONLINE":
            if value not in {b"TRUE", b"FALSE"}:
                raise StatusProtocolError("The printer returned an invalid online flag.")
            self._online = value == b"TRUE"
        elif name == b"DEVICESTATUS":
            self._device_status = self._parse_decimal_status_value(value, signed=True)
        elif name == b"CHARASET":
            if len(value) > 64 or not value.isascii():
                raise StatusProtocolError("The printer returned an invalid character-set identifier.")
            self._character_set = value.decode("ascii")
        elif name == b"DISPLAY":
            self._display_raw = value
            try:
                self._display = value.decode("utf-8")
                self._display_encoding = "utf-8"
            except UnicodeDecodeError:
                self._display = value.decode("latin-1")
                self._display_encoding = "latin-1-fallback"
        elif name == b"INFOMAINTE":
            self._parse_cartridge_status_record(value)
        else:
            return
        self._fields_seen += 1

    @staticmethod
    def _parse_decimal_status_value(value: bytes, *, signed: bool) -> int:
        pattern = _SIGNED_DECIMAL_VALUE if signed else _DECIMAL_VALUE
        if not pattern.fullmatch(value):
            raise StatusProtocolError("The printer returned a malformed decimal status value.")
        number = int(value, 10)
        if not -(2 ** 31) <= number <= 2 ** 31 - 1:
            raise StatusProtocolError("A printer status value exceeded its integer range.")
        return number

    def _parse_cartridge_status_record(self, value: bytes) -> None:
        components = value.split(b",")
        if components and not components[-1].strip():
            components.pop()
        if not 5 <= len(components) <= 64:
            raise StatusProtocolError("The cartridge status record had an invalid length.")
        decoded = bytearray()
        for component in components:
            component = component.strip(b" \t")
            if not _HEX_BYTE.fullmatch(component):
                raise StatusProtocolError("The cartridge status record contained an invalid hexadecimal byte.")
            decoded.append(int(component, 16))
        cartridge = _CARTRIDGE_TAGS.get(decoded[0])
        if cartridge is None:
            if len(self._unknown_tags) < 16:
                self._unknown_tags.append({"tag": f"0x{decoded[0]:02X}", "payload_hex": decoded.hex()})
            return
        current, maximum = decoded[3], decoded[4]
        percentage = stepped_ink_percentage(current, maximum)
        self._cartridges_seen.add(cartridge)
        self._cartridges[cartridge].update(
            percent=percentage, state="unknown" if percentage is None else "empty" if percentage == 0 else "reported",
            source=STATUS_SOURCE, raw_current=current, raw_maximum=maximum,
            raw_percent_floor=current * 100 // maximum if maximum else None,
            counter_within_maximum=current <= maximum if maximum else None,
        )
        if maximum == 0:
            self._record_warning("A cartridge reported a zero maximum counter; its level remains unknown.")
        elif current > maximum:
            self._record_warning("A cartridge counter exceeded its stated maximum; the original stepped display value is preserved.")

    def snapshot(self) -> dict:
        state, message = _STATUS_MESSAGES.get(self._status_code, ("unknown", ""))
        if state == "unknown":
            if self._online is False:
                state, message = "offline", "The printer reports that it is offline."
            elif self._status_code == 10001 and self._online is True:
                state, message = "ready", "The printer is online and ready."
        if not message:
            if self._display:
                message = self._display
            elif self._status_code is not None:
                message = f"The printer reported status code {self._status_code}."
            else:
                message = "The printer has not returned a recognized status code."
        return {
            "source": STATUS_SOURCE, "hardware_verified": False,
            "code": self._status_code, "state": state, "message": message,
            "online": self._online,
            "display": self._display,
            "display_raw_hex": self._display_raw.hex() if self._display_raw is not None else None,
            "display_encoding": self._display_encoding,
            "charset": self._character_set, "device_status": self._device_status,
            "ink": copy.deepcopy(self._cartridges),
            "granularity": "stepped", "percentage_steps": list(REPORTED_PERCENTAGE_STEPS),
            "frames": self.frame_count, "bytes_received": self.total_bytes,
            "complete_ink_and_status": self.has_complete_ink_and_status,
            "unknown_tags": copy.deepcopy(self._unknown_tags), "warnings": list(self._warnings),
        }

    def finish(self) -> dict:
        if not self._finished:
            self._remove_framing_prefix()
            if self._pending_line.strip(b"\r \t"):
                self._record_warning("An incomplete final printer status line was discarded.")
            self._pending_line.clear()
            self._finished = True
        if len(self._cartridges_seen) < 4:
            self._record_warning("The printer did not report a level for every cartridge.")
        return self.snapshot()


def parse_status_response(data: bytes) -> dict:
    parser = StatusResponseParser()
    parser.feed(data)
    return parser.finish()


def _build_status_command_packet(command: bytes) -> bytes:
    return COMMAND_PREFIX + command + UNIVERSAL_EXIT_LANGUAGE + COMMAND_PADDING


def _wait_for_descriptor(descriptor: int, deadline: float, *, writable: bool) -> bool:
    requested_event = select.POLLOUT if writable else select.POLLIN
    poller = select.poll()
    poller.register(descriptor, requested_event)
    while True:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return False
        try:
            notifications = poller.poll(max(1, math.ceil(remaining_seconds * 1000)))
        except InterruptedError:
            continue
        if not notifications:
            return False
        for _, events in notifications:
            if events & select.POLLNVAL:
                raise OSError(errno.EBADF, "The printer connection is no longer valid.")
            if events & select.POLLHUP:
                raise OSError(errno.ENODEV, "The printer connection was closed or disconnected.")
            if events & select.POLLERR:
                raise OSError(errno.EIO, "The printer connection reported an I/O error.")
            if events & requested_event:
                return True


def _write_command_completely(descriptor: int, command: bytes, deadline: float) -> None:
    """Send one packet and wait until the driver finishes its transfer.

    Driver readiness does not confirm that the printer acted on the command.
    """
    packet = memoryview(_build_status_command_packet(command))
    written = 0
    while written < len(packet):
        if not _wait_for_descriptor(descriptor, deadline, writable=True):
            raise TimeoutError("The printer did not accept its status command before the timeout.")
        try:
            count = os.write(descriptor, packet[written:])
        except (BlockingIOError, InterruptedError):
            continue
        if count <= 0:
            raise OSError(errno.EIO, "The printer accepted no status-command data.")
        written += count
    if not _wait_for_descriptor(descriptor, deadline, writable=True):
        raise TimeoutError("The printer status command did not finish sending before the timeout.")


def read_status_from_open_device(descriptor: int, *, timeout: float = 10.0,
                                 cleanup_timeout: float = 0.5,
                                 maximum_response_bytes: int = 32768) -> dict:
    """Poll an already checked, locked, nonblocking bidirectional printer.

    The cartridge channel is enabled and read before the printer-status channel.
    Each gets up to ``timeout`` seconds, including its command writes. The
    default is ten seconds per channel because this model can take several
    seconds to answer. Valid partial fields survive a channel timeout.

    Both notification-disable commands are attempted in ``finally`` with a
    shared ``cleanup_timeout`` deadline. The exchange budget is at most twice
    ``timeout`` plus ``cleanup_timeout``. Completed cleanup means the disable
    packets drained from the driver; it does not verify firmware processing.
    This function leaves ``hardware_verified`` false because its caller supplied
    the descriptor. No reset, driver detachment or protocol switch is performed.
    """
    if not 0.05 <= float(timeout) <= 30 or not 0.05 <= float(cleanup_timeout) <= 5:
        raise ValueError("Status timeout must be 0.05–30 seconds and cleanup timeout 0.05–5 seconds.")
    if not fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_NONBLOCK:
        raise ValueError("The printer descriptor must already be nonblocking.")
    parser = StatusResponseParser(maximum_response_bytes=maximum_response_bytes)
    exchange_started = time.monotonic()
    exchange_deadline = exchange_started + 2 * float(timeout)
    cartridge_deadline = exchange_started + float(timeout)
    primary_error: Exception | None = None
    cleanup_errors: list[str] = []
    channel_timeouts: list[str] = []
    read_recovery_used = False

    def read_channel_until_complete(deadline: float, completed: Callable[[], bool]) -> bool:
        nonlocal read_recovery_used
        while not completed() and _wait_for_descriptor(descriptor, deadline, writable=False):
            try:
                response = os.read(descriptor, min(4096, maximum_response_bytes - parser.total_bytes + 1))
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exception:
                # The driver attempts to restart failed USB reads.
                if exception.errno == errno.EIO and not read_recovery_used:
                    read_recovery_used = True
                    continue
                raise
            if not response:
                # An empty USB packet starts another read; it is not file EOF.
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds > 0:
                    time.sleep(min(_EMPTY_PACKET_PAUSE_SECONDS, remaining_seconds))
                continue
            parser.feed(response)
        return completed()

    try:
        # Start from the same notification state as the printer's Report Levels operation.
        for command in DISABLE_STATUS_COMMANDS:
            _write_command_completely(descriptor, command, cartridge_deadline)
        _write_command_completely(descriptor, ENABLE_STATUS_COMMANDS[1], cartridge_deadline)
        if not read_channel_until_complete(cartridge_deadline, lambda: parser.has_all_cartridges):
            channel_timeouts.append("ink")
            parser._record_warning("The printer did not report every cartridge before the cartridge-channel timeout.")

        status_deadline = min(time.monotonic() + float(timeout), exchange_deadline)
        _write_command_completely(descriptor, DISABLE_STATUS_COMMANDS[1], status_deadline)
        previous_status_frames = parser.completed_status_frames
        _write_command_completely(descriptor, ENABLE_STATUS_COMMANDS[0], status_deadline)
        # CODE precedes DISPLAY and ONLINE; finish that frame before stopping.
        if not read_channel_until_complete(status_deadline, lambda: parser.completed_status_frames > previous_status_frames):
            channel_timeouts.append("status")
            parser._record_warning("The printer did not finish its device-status response before the status-channel timeout.")
        if not parser.has_status_fields:
            raise TimeoutError("The printer did not return a complete status field before the channel timeouts.")
    except (OSError, StatusProtocolError) as exception:
        primary_error = exception
    finally:
        cleanup_deadline = min(time.monotonic() + float(cleanup_timeout), exchange_deadline + float(cleanup_timeout))
        for command in DISABLE_STATUS_COMMANDS:
            try:
                _write_command_completely(descriptor, command, cleanup_deadline)
            except (OSError, ValueError) as exception:
                channel = "DEVICE2" if b"DEVICE2" in command else "DEVICE"
                cleanup_errors.append(f"The {channel} status notification could not be disabled: {exception}")
    response_status = parser.finish()
    response_status["channel_timeouts"] = channel_timeouts
    if primary_error is not None:
        raise StatusTransportError(
            f"The bidirectional printer status exchange failed: {primary_error}",
            cleanup_errors=tuple(cleanup_errors), partial_status=response_status,
        ) from primary_error
    if cleanup_errors:
        raise StatusTransportError(
            "The printer returned status data, but status notifications could not be disabled.",
            cleanup_errors=tuple(cleanup_errors), partial_status=response_status,
        )
    response_status["cleanup_complete"] = True
    return response_status


def query_printer_status(device_path: str = "/dev/usb/lp0", **options) -> dict:
    """Query an identified printer while the caller holds its shared lock.

    A complete response with drained cleanup is marked ``hardware_verified``
    only here, after checking that this function opened a character device.
    """
    descriptor = os.open(device_path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise StatusTransportError("The configured printer path is not a character device.")
        result = read_status_from_open_device(descriptor, **options)
        if result["complete_ink_and_status"] and result["cleanup_complete"] and not result["channel_timeouts"]:
            result["hardware_verified"] = True
        return result
    finally:
        os.close(descriptor)
