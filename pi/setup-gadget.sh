#!/bin/sh
# Enable USB peripheral mode (Raspberry Pi) and install the c5-tunnel gadget
# service plus the klipper.service drop-in. Run with sudo from pi/.
set -e
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")"

ensure_line() {  # ensure_line FILE LINE
    grep -qxF "$2" "$1" 2>/dev/null || echo "$2" >> "$1"
}

if grep -qa "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
    CONFIG=/boot/firmware/config.txt
    [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
    OVERLAY=dtoverlay=dwc2,dr_mode=peripheral
    # Only count the overlay if it applies to every board: before the first
    # section filter or under [all]. Leave other sections (e.g. the stock
    # "[cm5] dtoverlay=dwc2,dr_mode=host") untouched.
    if ! awk -v want="$OVERLAY" '/^\[/ { s = $0 }
            $0 == want && (s == "" || s == "[all]") { found = 1 }
            END { exit !found }' "$CONFIG"; then
        printf '\n[all]\n%s\n' "$OVERLAY" >> "$CONFIG"
    fi
    ensure_line /etc/modules dwc2
    echo "configured dwc2 peripheral mode in $CONFIG"
else
    echo "not a Raspberry Pi: enable this board's USB device (UDC/OTG" \
         "peripheral) overlay yourself; any board with an entry in" \
         "/sys/class/udc works"
fi
ensure_line /etc/modules libcomposite

install -m 755 c5-tunnel-gadget.sh /usr/local/sbin/c5-tunnel-gadget.sh
install -m 644 c5-tunnel-gadget.service /etc/systemd/system/c5-tunnel-gadget.service
install -d /etc/systemd/system/klipper.service.d
install -m 644 klipper-c5-tunnel.conf /etc/systemd/system/klipper.service.d/c5-tunnel.conf
systemctl daemon-reload
systemctl enable c5-tunnel-gadget.service

if [ -n "$(ls /sys/class/udc 2>/dev/null)" ]; then
    systemctl start c5-tunnel-gadget.service
    ls -l /dev/ttyGS*
else
    echo "reboot required (no UDC yet); then check: ls -l /dev/ttyGS*"
fi
