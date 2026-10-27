#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail
purge=0
staging_root=
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --purge-config) purge=1; shift ;;
        --staging-root)
            [ "$#" -ge 2 ] && [ -n "$2" ] || fail '--staging-root requires an absolute directory'
            staging_root=$(realpath -m -- "$2")
            [[ $2 == /* && $staging_root != / ]] || fail 'the staging directory must be absolute and must not be the filesystem root'
            shift 2
            ;;
        -h|--help)
            echo 'Usage: sudo scripts/uninstall.sh [--purge-config] [--staging-root DIRECTORY]'
            exit 0
            ;;
        *) fail "unknown option: $1; use --help to see the removal options" ;;
    esac
done
if [ -z "$staging_root" ]; then
    [ "$(id -u)" -eq 0 ] || fail 'run this removal command with sudo'
    systemctl disable --now dcp150c-dashboard.service 2>/dev/null || true
    if [ -f /var/lib/dcp150c-native/queues.tsv ]; then
        while IFS=$'\t' read -r queue saved_uri extra; do
            [[ $queue =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$ && -z $extra ]] || continue
            case "$saved_uri" in
                dcp150c:usb://Brother/DCP-150C|dcp150c:usb://Brother/DCP-150C\?*) ;;
                *) continue ;;
            esac
            current=$(LC_ALL=C lpstat -v "$queue" 2>/dev/null || true)
            if [ "$current" = "device for $queue: $saved_uri" ]; then
                lpadmin -x "$queue"
                printf 'Removed the native CUPS queue %s.\n' "$queue"
            elif [ -n "$current" ]; then
                printf 'Preserving queue %s because its device URI was changed.\n' "$queue"
            fi
        done < /var/lib/dcp150c-native/queues.tsv
    elif [ -f /var/lib/dcp150c-native/queue ]; then
        printf 'The older queue record has no exact device URI; existing CUPS queues were preserved.\n'
    fi
else
    command -v python3 >/dev/null || fail 'python3 is required to check the staging directory'
    python3 - "$staging_root" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
for relative in ('usr/bin', 'usr/lib', 'usr/share', 'etc', 'var/lib',
                 'usr/lib/systemd/system', 'usr/lib/tmpfiles.d', 'etc/udev/rules.d',
                 'usr/lib/cups/filter', 'usr/lib/cups/backend'):
    if not (root / relative).resolve().is_relative_to(root):
        sys.exit(f'Error: staging path points outside the staging directory: {root / relative}')
PY
fi
cups_serverbin=/usr/lib/cups
if [ -z "$staging_root" ] && command -v cups-config >/dev/null; then
    cups_serverbin=$(cups-config --serverbin)
fi
configuration_command=$staging_root/usr/bin/dcp150c-config
if [ -L "$configuration_command" ] && \
    [ "$(readlink -- "$configuration_command")" = /usr/lib/dcp150c-native/brprintconf_dcp150c ]; then
    rm -f -- "$configuration_command"
elif [ -e "$configuration_command" ] || [ -L "$configuration_command" ]; then
    printf 'Preserving dcp150c-config because it belongs to another installation.\n'
fi
rm -f -- "$staging_root/usr/lib/systemd/system/dcp150c-dashboard.service" \
    "$staging_root/usr/lib/tmpfiles.d/dcp150c-native.conf" \
    "$staging_root/etc/udev/rules.d/70-dcp150c-native.rules" \
    "$staging_root$cups_serverbin/filter/dcp150c-filter" \
    "$staging_root$cups_serverbin/backend/dcp150c"
rm -rf -- "$staging_root/usr/lib/dcp150c-native" \
    "$staging_root/usr/share/dcp150c-native" \
    "$staging_root/usr/share/ppd/dcp150c-native" \
    "$staging_root/var/lib/dcp150c-native"
if [ "$purge" -eq 1 ]; then
    rm -rf -- "$staging_root/etc/dcp150c-native"
fi
if [ -z "$staging_root" ]; then
    systemctl daemon-reload
    udevadm control --reload-rules
fi
# Keep the service account and active lock inode: neither should be recycled
# while a previous process might still hold the lock or own device files.
if [ "$purge" -eq 1 ]; then
    printf 'Software and configuration were removed.\n'
else
    printf 'Software was removed. Your settings remain in /etc/dcp150c-native.\n'
fi
