#!/bin/sh
# Restore the stock firmwareExe. --purge also removes /usr/prog/c5-tunnel.
set -e
DIR=/usr/prog/c5-tunnel
TARGET=/usr/prog/PROGRAM/software/firmwareExe
STOCK=$DIR/firmwareExe.stock

die() { echo "uninstall: $*" >&2; exit 1; }
[ -f "$STOCK" ] || die "$STOCK not found"
[ "$(dd if="$STOCK" bs=1 skip=1 count=3 2>/dev/null)" = "ELF" ] \
    || die "$STOCK is not an ELF binary"

# Rename instead of overwriting the running launcher script.
cp -p "$STOCK" "$TARGET.stock-restore"
mv -f "$TARGET.stock-restore" "$TARGET"
sync
sha256sum "$TARGET"
if [ "$1" = "--purge" ]; then
    rm -rf "$DIR"
    sync
    echo "removed $DIR"
fi
echo "stock firmwareExe restored; reboot to return to the stock UI"
