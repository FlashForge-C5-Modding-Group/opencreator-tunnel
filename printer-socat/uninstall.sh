#!/bin/sh
# Restore exactly the launcher that was active before socat installation.
set -e
DIR=/usr/prog/c5-tunnel-socat
TARGET=/usr/prog/PROGRAM/software/firmwareExe
[ -s "$DIR/firmwareExe.previous" ] || {
    echo "socat rollback: previous launcher missing; unchanged" >&2
    exit 1
}
grep -q 'Opt-in socat launcher' "$TARGET" || {
    echo "socat rollback: active launcher changed; refusing to overwrite it" >&2
    exit 1
}
cp -p "$DIR/firmwareExe.previous" "$TARGET.c5-restore"
mv -f "$TARGET.c5-restore" "$TARGET"
sync
echo "previous launcher restored; reboot to activate"
