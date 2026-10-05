#!/bin/sh
# Install the c5-tunnel launcher over the stock firmwareExe.
# Run as root on the printer from /usr/prog/c5-tunnel.
set -e
DIR=/usr/prog/c5-tunnel
SOFTWARE=/usr/prog/PROGRAM/software
TARGET=$SOFTWARE/firmwareExe
PYTHON=/usr/prog/Python-3.8.2/bin/python3
# Same library path as the launcher (and stock klipper/start.sh).
export LD_LIBRARY_PATH=/usr/prog/Python-3.8.2/lib:/usr/prog/openssl-1.0.2d/lib:/usr/prog/libffi-3.4.4/lib:$LD_LIBRARY_PATH

die() { echo "install: $*" >&2; exit 1; }
is_elf() { [ "$(dd if="$1" bs=1 skip=1 count=3 2>/dev/null)" = "ELF" ]; }

cd "$(dirname "$0")"
[ "$(pwd)" = "$DIR" ] || die "copy this directory to $DIR and run it from there"
[ -f "$TARGET" ] || die "$TARGET not found"
[ -f c5_bridge.py ] && [ -f firmwareExe ] || die "c5_bridge.py/firmwareExe missing in $DIR"
"$PYTHON" -c 'import termios, select' || die "$PYTHON does not run"
"$PYTHON" -c 'import c5_bridge, sys; sys.exit(0 if c5_bridge.ensure_urb_reader_binary() else 1)' \
    || die "could not build the USB URB reader; check /opt/bin/gcc and /opt/include"

if is_elf "$TARGET"; then
    if [ -e firmwareExe.stock ]; then
        echo "stock backup already present; keeping it"
    else
        cp -p "$TARGET" firmwareExe.stock
        echo "backed up stock firmwareExe to $DIR/firmwareExe.stock"
    fi
    VERSION=$(cd "$SOFTWARE" && ls -d [0-9]* 2>/dev/null | tail -n 1)
    sha256sum firmwareExe.stock
    [ -n "$VERSION" ] && [ -f "$SOFTWARE/$VERSION/firmwareExe" ] \
        && sha256sum "$SOFTWARE/$VERSION/firmwareExe"
elif grep -q 'c5-tunnel launcher' "$TARGET"; then
    echo "launcher already installed; updating it"
else
    die "$TARGET is neither the stock binary nor the c5-tunnel launcher"
fi
[ -e firmwareExe.stock ] || die "no stock backup; refusing to replace $TARGET"

# Rename instead of overwriting: the running stock binary is text-busy.
cp firmwareExe "$TARGET.c5-tunnel"
chmod 755 "$TARGET.c5-tunnel" c5_bridge.py uninstall.sh
mv -f "$TARGET.c5-tunnel" "$TARGET"
sync
echo "reboot to activate; log: /usr/data/logs/c5-tunnel.log"
