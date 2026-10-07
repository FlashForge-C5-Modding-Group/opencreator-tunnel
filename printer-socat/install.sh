#!/bin/sh
# Run from /usr/prog/c5-tunnel-socat on the printer, as root.
set -e
DIR=/usr/prog/c5-tunnel-socat
TARGET=/usr/prog/PROGRAM/software/firmwareExe
PYTHON=/usr/prog/Python-3.8.2/bin/python3
die() { echo "socat install: $*" >&2; exit 1; }

cd "$(dirname "$0")"
[ "$(pwd)" = "$DIR" ] || die "copy this folder to $DIR first"
[ -f "$TARGET" ] || die "$TARGET missing"
[ -f firmwareExe ] && [ -f c5_socat.py ] && [ -f identify_monitor.c ] \
    || die "incomplete socat folder"
if [ -e firmwareExe.previous ]; then
    grep -q 'Opt-in socat launcher' "$TARGET" \
        || die "target changed since the previous backup; refusing to overwrite it"
elif grep -q 'Opt-in socat launcher' "$TARGET"; then
    die "socat launcher is active but its previous-launcher backup is missing"
fi
if command -v socat >/dev/null 2>&1; then
    :
elif [ -x /opt/bin/socat ]; then
    :
else
    die "socat is not installed; unchanged"
fi
if command -v cc >/dev/null 2>&1; then
    CC=cc
elif [ -x /opt/bin/gcc ]; then
    CC=/opt/bin/gcc
else
    die "no C compiler found; unchanged"
fi
"$PYTHON" -m py_compile c5_socat.py || die "Python syntax check failed; unchanged"
"$CC" -std=c99 -O2 -Wall -Wextra -Werror \
    -o identify_monitor.new identify_monitor.c \
    || die "monitor build failed; unchanged"
chmod 755 identify_monitor.new
mv -f identify_monitor.new identify_monitor

# Never overwrite an existing saved launcher; this preserves exact rollback.
if [ ! -e firmwareExe.previous ]; then
    cp -p "$TARGET" firmwareExe.previous
fi
[ -s firmwareExe.previous ] || die "previous launcher backup missing or empty"
cp firmwareExe "$TARGET.c5-socat"
chmod 755 "$TARGET.c5-socat" c5_socat.py uninstall.sh
mv -f "$TARGET.c5-socat" "$TARGET"
sync
echo "socat tunnel installed; reboot to activate"
echo "rollback: sh $DIR/uninstall.sh && reboot"
echo "log: /usr/data/logs/c5-socat.log"
