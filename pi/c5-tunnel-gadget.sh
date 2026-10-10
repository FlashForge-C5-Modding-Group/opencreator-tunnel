#!/bin/sh
# Create a 4- or 5-port USB serial gadget (/dev/ttyGS0..3 or ..4) through
# configfs. Uses "gser" (two bulk endpoints per port): four CDC-ACM ports
# need eight IN endpoints, more than the Raspberry Pi's dwc2 controller has.
# Port n is USB interface n on the printer, which binds it to a usb-serial
# driver; c5_bridge.py maps ports 0-3 to one MCU each, and port 4
# (ttyGS4/ttyUSB4, when present) to the beep control channel (see
# BEEP_SERIAL/the BeepServer in c5_bridge.py and c5_socat.py) so C5_BUZZER
# works over the USB link itself, without needing the printer's own
# network/Wi-Fi up.
#
# Not every board's USB peripheral controller tolerates 5 active gser
# functions under sustained traffic: a Raspberry Pi 4 (dwc2) runs fine, but
# an Orange Pi Zero 2W's own OTG controller was observed enumerating all 5
# fine, then destabilizing the whole bus after a few minutes (-71/EPROTO
# write errors cascading into a full USB disconnect on all ports) -- 4
# ports stayed solid on the same hardware. That failure only shows up under
# runtime traffic, so this script can't detect it; if you hit it, pin the
# board to 4 ports permanently with "echo 4 | sudo tee /etc/c5-tunnel-ports"
# (run "stop" first if the gadget is already bound, since this script is
# idempotent and won't reconfigure an already-bound gadget).
#
# What this script CAN detect and recover from automatically is an
# outright bind failure at setup time (an immediate I/O error writing to
# UDC, meaning the controller rejected the 5-port descriptor set): if
# C5_TUNNEL_PORTS/the ports file does not already pin a specific count,
# it tries 5 first and falls back to 4 on that failure, without needing a
# retry or any manual step.
#
# Idempotent; "stop" unbinds the gadget from its UDC.
set -e
G=/sys/kernel/config/usb_gadget/c5tunnel
PORTS_FILE=/etc/c5-tunnel-ports
PORTS_PINNED=1
if [ -n "$C5_TUNNEL_PORTS" ]; then
    PORTS=$C5_TUNNEL_PORTS
elif [ -r "$PORTS_FILE" ]; then
    PORTS=$(cat "$PORTS_FILE")
else
    PORTS=5
    PORTS_PINNED=0
fi
case "$PORTS" in
    4|5) ;;
    *) echo "C5_TUNNEL_PORTS/$PORTS_FILE must be 4 or 5, got '$PORTS'" >&2; exit 1 ;;
esac
modprobe libcomposite 2>/dev/null || true
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
if [ "$1" = stop ]; then
    [ -f $G/UDC ] && echo "" > $G/UDC
    exit 0
fi
if [ -n "$(cat $G/UDC 2>/dev/null)" ]; then
    exit 0
fi

bind_ports() {  # bind_ports PORTS
    last=$(($1 - 1))
    mkdir -p $G
    cd $G
    echo 0x1d6b > idVendor
    echo 0x0104 > idProduct
    echo 0x0100 > bcdDevice
    echo 0x0200 > bcdUSB
    mkdir -p strings/0x409
    echo c5tunnel > strings/0x409/serialnumber
    echo c5-tunnel > strings/0x409/manufacturer
    echo "Creator 5 MCU tunnel" > strings/0x409/product
    mkdir -p configs/c.1/strings/0x409
    echo "C5 tunnel" > configs/c.1/strings/0x409/configuration
    # Drop functions left by an older layout (e.g. acm.*, or gser.usb4 from
    # a previous higher-PORTS run): anything outside the wanted 0..last
    # range would exceed the controller's endpoints or just leave a
    # stale/unused port and make the bind fail or misbehave.
    for f in configs/c.1/*.*; do
        [ -L "$f" ] || continue
        case "${f##*/}" in
            gser.usb[0-$last]) ;;
            *) rm "$f"; rmdir "functions/${f##*/}" 2>/dev/null || true ;;
        esac
    done
    # Create and link in order so usb<n> gets ttyGS<n> and interface n.
    i=0
    while [ "$i" -le "$last" ]; do
        mkdir -p functions/gser.usb$i
        [ -e configs/c.1/gser.usb$i ] || ln -s functions/gser.usb$i configs/c.1/
        i=$((i + 1))
    done
    udc=$(ls /sys/class/udc | head -n 1)
    if [ -z "$udc" ]; then
        echo "no UDC in /sys/class/udc (USB peripheral mode not enabled)" >&2
        return 1
    fi
    echo "$udc" > UDC
}

if bind_ports "$PORTS" 2>/tmp/c5-tunnel-gadget.err; then
    exit 0
fi
cat /tmp/c5-tunnel-gadget.err >&2
if [ "$PORTS" -eq 5 ] && [ "$PORTS_PINNED" -eq 0 ]; then
    echo "5-port bind failed; falling back to 4 ports automatically" >&2
    if bind_ports 4; then
        echo "4 | pinning via $PORTS_FILE for future boots" >&2
        echo 4 > "$PORTS_FILE" 2>/dev/null || true
        exit 0
    fi
fi
exit 1
