#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
dashboard_only=0
start_service=1
configure_queue=1
share_lan=0
queue=DCP150C-Native
queue_explicit=0
device_uri=
staging_root=
native_filter=$project_dir/build/bh7filter
native_configuration_tool=
cups_serverbin=/usr/lib/cups
reuse_queue=0
allow_unverified_filter=0
filter_verification=

usage() {
    cat <<'USAGE'
Usage: sudo scripts/install.sh [options]
  --dashboard-only         Install status/dashboard without a native print queue
  --filter FILE            Native bh7filter; brprintconf_dcp150c must be beside it
  --allow-unverified-filter Approve the exact executable of a functional preview build
  --queue NAME             CUPS queue name (default: DCP150C-Native)
  --device-uri URI         usb://Brother/DCP-150C?... from lpinfo -v
  --no-queue               Install CUPS files without configuring a queue
  --share-lan              Share the new queue with local-network IPP clients
  --no-start               Install without starting/enabling the dashboard
  --staging-root DIRECTORY Stage files only; do not change users, queues or services
USAGE
}

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
require_arg() { [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 requires a value"; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dashboard-only) dashboard_only=1; configure_queue=0; shift ;;
        --filter) require_arg "$@"; native_filter=$2; shift 2 ;;
        --allow-unverified-filter) allow_unverified_filter=1; shift ;;
        --queue) require_arg "$@"; queue=$2; queue_explicit=1; shift 2 ;;
        --device-uri) require_arg "$@"; device_uri=$2; shift 2 ;;
        --no-queue) configure_queue=0; shift ;;
        --share-lan) share_lan=1; shift ;;
        --no-start) start_service=0; shift ;;
        --staging-root) require_arg "$@"; staging_root=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $1; use --help to see the installation options" ;;
    esac
done

[[ $queue =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$ ]] || fail 'the queue name must contain only letters, digits, dots, underscores or hyphens'
if [ -n "$staging_root" ]; then
    [[ $staging_root == /* ]] || fail '--staging-root must be an absolute path'
    staging_root=$(realpath -m -- "$staging_root")
    [ "$staging_root" != / ] || fail 'the filesystem root is not a staging directory'
    configure_queue=0
    start_service=0
else
    [ "$(id -u)" -eq 0 ] || fail 'run this installer with sudo'
fi

command -v python3 >/dev/null || fail 'install python3 first'
if [ "$dashboard_only" -eq 1 ] && [ "$allow_unverified_filter" -eq 1 ]; then
    fail '--allow-unverified-filter applies to a full native printing installation'
fi
if [ "$queue_explicit" -eq 1 ] && [ -L "$staging_root/etc/dcp150c-native/queue.env" ]; then
    fail 'queue.env is a symbolic link; edit its queue selection directly instead of replacing it'
fi
for source in dcp150c/__main__.py dcp150c/server.py dcp150c/status.py dcp150c/static/index.html \
    config/default.conf config/paperinf LICENSE packaging/systemd/dashboard.env; do
    [ -f "$project_dir/$source" ] || fail "required software file missing: $source"
done
if [ "$dashboard_only" -eq 0 ]; then
    [ -x "$native_filter" ] || fail 'build the native raster filter first, or use --dashboard-only'
    native_filter=$(realpath -e -- "$native_filter")
    native_configuration_tool=$(dirname -- "$native_filter")/brprintconf_dcp150c
    [ -x "$native_configuration_tool" ] || fail 'the matching brprintconf_dcp150c executable must be beside bh7filter; build and copy both executables together'
    for source in data/calibration.dat data/brio06aa.bcm data/brio06ab.bcm data/brio06ac.bcm \
        data/brio06af.bcm data/brio06ag.bcm data/BROTHER-LICENSE.txt \
        LICENSES/musl-COPYRIGHT.txt LICENSES/GCC-runtime-COPYRIGHT.txt; do
        [ -f "$project_dir/$source" ] || fail "required runtime file is missing: $source"
    done
    # Do not install a placeholder or a cross-compiled binary for another CPU.
    filter_verification=$(python3 - "$native_filter" "$native_configuration_tool" "$allow_unverified_filter" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

capabilities = None
try:
    result = subprocess.run([sys.argv[1], '--capabilities'], capture_output=True, check=True, timeout=10)
    capabilities = json.loads(result.stdout)
    ready = isinstance(capabilities, dict) and capabilities.get('production_ready') is True
except (OSError, subprocess.SubprocessError, ValueError):
    ready = False
functional_preview = (
    isinstance(capabilities, dict) and capabilities.get('production_ready') is False
    and capabilities.get('model') == 'Brother DCP-150C'
    and capabilities.get('native_raster') is True
    and isinstance(capabilities.get('qualities'), list)
    and all(isinstance(quality, str) for quality in capabilities['qualities'])
    and {'Draft', 'Fast', 'Normal', 'Fine', 'Photo', 'Highest'}.issubset(capabilities['qualities'])
)
if not ready and not (sys.argv[3] == '1' and functional_preview):
    sys.exit('Error: native filter is unavailable or has not passed production verification; installation stopped.')

try:
    result = subprocess.run([sys.argv[2], '--help'], capture_output=True, check=True, timeout=10)
    if b'DCP150C' not in result.stdout or b'--config' not in result.stdout:
        raise ValueError('unrecognized configuration tool')
    headers = [Path(path).read_bytes()[:20] for path in sys.argv[1:3]]
    if all(header.startswith(b'\x7fELF') for header in headers):
        architectures = [(header[4:6], header[18:20]) for header in headers]
        if architectures[0] != architectures[1]:
            raise ValueError('different executable architectures')
except (OSError, subprocess.SubprocessError, ValueError):
    sys.exit('Error: the matching DCP150C configuration tool cannot run on this computer; installation stopped.')
print('verified' if ready else 'preview')
PY
)
    if [ -z "$staging_root" ]; then
        for command in gs lpadmin lpstat lpinfo flock; do
            command -v "$command" >/dev/null || fail "missing $command; install cups, cups-client, ghostscript and util-linux"
        done
        if command -v cups-config >/dev/null; then
            cups_serverbin=$(cups-config --serverbin)
        fi
        [ -x "$cups_serverbin/backend/usb" ] || fail 'the CUPS USB backend is not installed'
    fi
    configuration_command=$staging_root/usr/bin/dcp150c-config
    if [ -e "$configuration_command" ] || [ -L "$configuration_command" ]; then
        [ -L "$configuration_command" ] && \
            [ "$(readlink -- "$configuration_command")" = /usr/lib/dcp150c-native/brprintconf_dcp150c ] || \
            fail 'dcp150c-config already belongs to another installation; the existing command was preserved'
    fi
    [ ! -L "$staging_root/etc/dcp150c-native/allow-unverified-filter" ] || \
        fail 'the preview approval file is a symbolic link; replace it with a regular file before installing'
    if [ -e "$staging_root/etc/dcp150c-native/allow-unverified-filter" ] && \
        [ ! -f "$staging_root/etc/dcp150c-native/allow-unverified-filter" ]; then
        fail 'the preview approval path must be a regular file'
    fi
fi

if [ "$share_lan" -eq 1 ] && { [ "$dashboard_only" -eq 1 ] || [ "$configure_queue" -eq 0 ]; }; then
    fail '--share-lan requires native printing and a queue'
fi
if [ "$share_lan" -eq 1 ]; then
    command -v cupsctl >/dev/null || fail 'CUPS sharing requires cupsctl; install cups-client first'
fi

if [ "$configure_queue" -eq 1 ]; then
    if [ -z "$device_uri" ]; then
        mapfile -t discovered < <(LC_ALL=C lpinfo -v | awk '$1 == "direct" && $2 ~ /^usb:\/\/Brother\/DCP-150C(\?|$)/ { print $2 }')
        [ "${#discovered[@]}" -eq 1 ] || fail 'connect and power on exactly one DCP-150C, or supply --device-uri'
        device_uri=${discovered[0]}
    fi
    device_uri=${device_uri#dcp150c:}
    case "$device_uri" in
        usb://Brother/DCP-150C|usb://Brother/DCP-150C\?*) ;;
        *) fail 'device URI must identify a USB Brother DCP-150C' ;;
    esac
    [[ $device_uri != *[[:space:][:cntrl:]]* ]] || fail 'the device URI must not contain unescaped spaces or control characters'
    existing=$(LC_ALL=C lpstat -v "$queue" 2>/dev/null || true)
    if [ -n "$existing" ]; then
        if [ "$existing" != "device for $queue: dcp150c:$device_uri" ] || \
            [ ! -f /var/lib/dcp150c-native/queues.tsv ] || \
            ! awk -F '\t' -v name="$queue" -v uri="dcp150c:$device_uri" \
                '$1 == name && $2 == uri && NF == 2 { found = 1 } END { exit !found }' /var/lib/dcp150c-native/queues.tsv; then
            fail "queue $queue already exists without a matching installation record; choose another name to preserve it"
        fi
        reuse_queue=1
    fi
    # An enabled legacy queue bypasses our shared lock, so do not create a race.
    while IFS= read -r line; do
        other_queue=${line#device for }
        other_queue=${other_queue%%:*}
        other_uri=${line#*: }
        if [ "$other_queue" != "$queue" ] && [ "$other_uri" = "$device_uri" ]; then
            other_state=$(LC_ALL=C lpstat -p "$other_queue" 2>/dev/null || true)
            [[ $other_state == *" disabled "* ]] || fail "legacy queue $other_queue uses this USB device; disable it before installing the native queue"
        fi
    done < <(LC_ALL=C lpstat -v 2>/dev/null || true)
fi

# A staging directory must not redirect installation through a host symlink.
if [ -n "$staging_root" ]; then
    python3 - "$staging_root" <<'PY'
from pathlib import Path
import os
import sys

root = Path(sys.argv[1])
directories = ('usr/bin', 'usr/lib/dcp150c-native', 'usr/share/dcp150c-native',
               'usr/lib/systemd/system', 'usr/lib/tmpfiles.d', 'etc/dcp150c-native',
               'etc/udev/rules.d', 'usr/lib/cups/filter', 'usr/lib/cups/backend',
               'usr/share/ppd/dcp150c-native')
for relative in directories:
    directory = root / relative
    candidates = [directory]
    if directory.is_dir() and relative.endswith('dcp150c-native'):
        for parent, children, _ in os.walk(directory, followlinks=False):
            candidates.extend(Path(parent) / child for child in children)
    for candidate in candidates:
        if not candidate.resolve().is_relative_to(root):
            sys.exit(f'Error: staging path points outside the staging directory: {candidate}')
PY
fi

if [ -z "$staging_root" ]; then
    for command in systemctl systemd-tmpfiles udevadm getent; do
        command -v "$command" >/dev/null || fail "required Raspberry Pi OS command is missing: $command"
    done
    getent group dcp150c >/dev/null || groupadd --system dcp150c
    id -u dcp150c >/dev/null 2>&1 || useradd --system --gid dcp150c --home-dir /nonexistent --shell /usr/sbin/nologin dcp150c
    [ "$(id -gn dcp150c)" = dcp150c ] || fail 'existing dcp150c user has a different primary group'
fi

install -d -m 0755 "$staging_root/usr/lib/dcp150c-native/dcp150c" \
    "$staging_root/usr/share/dcp150c-native" "$staging_root/etc/dcp150c-native" \
    "$staging_root/usr/lib/systemd/system" "$staging_root/usr/lib/tmpfiles.d" \
    "$staging_root/etc/udev/rules.d"
while IFS= read -r -d '' source; do
    relative=${source#"$project_dir/dcp150c/"}
    install -D -m 0644 "$source" "$staging_root/usr/lib/dcp150c-native/dcp150c/$relative"
done < <(find "$project_dir/dcp150c" -type f \( -name '*.py' -o -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.svg' \) -print0)
if [ ! -e "$staging_root/etc/dcp150c-native/default.conf" ] && [ ! -L "$staging_root/etc/dcp150c-native/default.conf" ]; then
    install -m 0644 "$project_dir/config/default.conf" "$staging_root/etc/dcp150c-native/default.conf"
fi
if [ ! -e "$staging_root/etc/dcp150c-native/dashboard.env" ] && [ ! -L "$staging_root/etc/dcp150c-native/dashboard.env" ]; then
    install -m 0644 "$project_dir/packaging/systemd/dashboard.env" "$staging_root/etc/dcp150c-native/dashboard.env"
fi
install -m 0644 "$project_dir/config/paperinf" "$staging_root/usr/share/dcp150c-native/paperinf"
install -m 0644 "$project_dir/LICENSE" "$staging_root/usr/share/dcp150c-native/LICENSE"
install -m 0644 "$project_dir/packaging/systemd/dcp150c-dashboard.service" "$staging_root/usr/lib/systemd/system/"
install -m 0644 "$project_dir/packaging/tmpfiles/dcp150c-native.conf" "$staging_root/usr/lib/tmpfiles.d/"
install -m 0644 "$project_dir/packaging/udev/70-dcp150c-native.rules" "$staging_root/etc/udev/rules.d/"
if [ "$queue_explicit" -eq 1 ] || { [ "$configure_queue" -eq 1 ] && \
    [ ! -e "$staging_root/etc/dcp150c-native/queue.env" ] && [ ! -L "$staging_root/etc/dcp150c-native/queue.env" ]; }; then
    printf 'DCP150C_QUEUE=%s\n' "$queue" > "$staging_root/etc/dcp150c-native/queue.env"
    chmod 0644 "$staging_root/etc/dcp150c-native/queue.env"
fi
if [ "$dashboard_only" -eq 0 ]; then
    install -m 0755 "$native_filter" "$staging_root/usr/lib/dcp150c-native/bh7filter"
    install -m 0755 "$native_configuration_tool" "$staging_root/usr/lib/dcp150c-native/brprintconf_dcp150c"
    install -d -m 0755 "$staging_root/usr/bin" "$staging_root/usr/share/dcp150c-native/LICENSES"
    if [ ! -L "$configuration_command" ]; then
        ln -s /usr/lib/dcp150c-native/brprintconf_dcp150c "$configuration_command"
    fi
    for source in calibration.dat brio06aa.bcm brio06ab.bcm brio06ac.bcm brio06af.bcm brio06ag.bcm BROTHER-LICENSE.txt; do
        install -m 0644 "$project_dir/data/$source" "$staging_root/usr/share/dcp150c-native/$source"
    done
    install -m 0644 "$project_dir/LICENSES/musl-COPYRIGHT.txt" "$project_dir/LICENSES/GCC-runtime-COPYRIGHT.txt" \
        "$staging_root/usr/share/dcp150c-native/LICENSES/"
    if [ "$filter_verification" = preview ]; then
        python3 - "$staging_root/etc/dcp150c-native/allow-unverified-filter" "$native_filter" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

approval_path = Path(sys.argv[1])
approval = {'format': 1, 'model': 'Brother DCP-150C',
            'filter_sha256': hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest()}
with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', dir=approval_path.parent, delete=False) as output:
    json.dump(approval, output, sort_keys=True)
    output.write('\n')
    os.fchmod(output.fileno(), 0o644)
    if os.geteuid() == 0:
        os.fchown(output.fileno(), 0, 0)
os.replace(output.name, approval_path)
PY
        printf 'Functional preview build selected explicitly. See README.md for tested hardware coverage.\n'
    else
        rm -f -- "$staging_root/etc/dcp150c-native/allow-unverified-filter"
    fi
    install -D -m 0755 "$project_dir/packaging/cups/dcp150c-filter" "$staging_root$cups_serverbin/filter/dcp150c-filter"
    # CUPS runs restricted-permission backends as root, as required by USB.
    install -D -m 0700 "$project_dir/packaging/cups/dcp150c" "$staging_root$cups_serverbin/backend/dcp150c"
    install -D -m 0644 "$project_dir/packaging/cups/dcp150c-native.ppd" "$staging_root/usr/share/ppd/dcp150c-native/dcp150c-native.ppd"
fi

if [ -n "$staging_root" ]; then
    printf 'Software files staged in %s; host users, services and queues were not changed.\n' "$staging_root"
    exit 0
fi

systemd-tmpfiles --create /usr/lib/tmpfiles.d/dcp150c-native.conf
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=04f9 --attr-match=idProduct=01cf
udevadm trigger --subsystem-match=usbmisc --sysname-match='lp*'
# Refresh card-node read permissions only beneath this printer's USB device.
python3 - <<'PY'
from pathlib import Path
import subprocess

for device in Path('/sys/bus/usb/devices').iterdir():
    try:
        matches = ((device / 'idVendor').read_text().strip() == '04f9'
                   and (device / 'idProduct').read_text().strip() == '01cf')
    except OSError:
        continue
    if matches:
        subprocess.run(['udevadm', 'trigger', '--subsystem-match=block',
                        '--parent-match=' + str(device.resolve())], check=True)
PY
if [ "$configure_queue" -eq 1 ]; then
    if [ "$reuse_queue" -eq 0 ]; then
        lpadmin -p "$queue" -E -v "dcp150c:$device_uri" -P /usr/share/ppd/dcp150c-native/dcp150c-native.ppd -o printer-is-shared=false
    fi
    install -d -m 0755 /var/lib/dcp150c-native
    queue_records=$(mktemp /var/lib/dcp150c-native/queues.XXXXXX)
    if [ -f /var/lib/dcp150c-native/queues.tsv ]; then
        awk -F '\t' -v name="$queue" '$1 != name' /var/lib/dcp150c-native/queues.tsv > "$queue_records"
    fi
    printf '%s\t%s\n' "$queue" "dcp150c:$device_uri" >> "$queue_records"
    chmod 0644 "$queue_records"
    mv -f -- "$queue_records" /var/lib/dcp150c-native/queues.tsv
    printf '%s\n' "$queue" > /var/lib/dcp150c-native/queue
    if [ "$share_lan" -eq 1 ]; then
        cupsctl --share-printers
        lpadmin -p "$queue" -o printer-is-shared=true
    fi
    if [ "$reuse_queue" -eq 1 ]; then
        printf 'Existing native CUPS queue %s was preserved with its saved printer options.\n' "$queue"
    else
        printf 'Native CUPS queue %s is ready for print jobs.\n' "$queue"
    fi
fi
systemctl daemon-reload
if [ "$start_service" -eq 1 ]; then
    systemctl enable dcp150c-dashboard.service
    systemctl restart dcp150c-dashboard.service
    systemctl is-active --quiet dcp150c-dashboard.service || fail 'dashboard did not start; inspect journalctl -u dcp150c-dashboard'
fi
printf 'Installation completed. Open the dashboard at http://<this-pi-address>:8080/.\n'
if [ "$dashboard_only" -eq 1 ]; then
    printf 'Dashboard-only installation: existing printer queues were not changed.\n'
fi
