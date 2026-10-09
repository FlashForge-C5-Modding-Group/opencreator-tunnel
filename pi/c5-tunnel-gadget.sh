#!/bin/sh
# Create a five-port USB serial gadget (/dev/ttyGS0..4) through configfs.
# Uses "gser" (two bulk endpoints per port): four CDC-ACM ports need eight
# IN endpoints, more than the Raspberry Pi's dwc2 controller has.
# Port n is USB interface n (1.0 .. 1.4) on the printer, which binds it to
# a usb-serial driver; c5_bridge.py maps ports 0-3 to one MCU each, and
# port 4 (ttyGS4/ttyUSB4) to the beep control channel (see
# BEEP_SERIAL/the BeepServer in c5_bridge.py and c5_socat.py) so C5_BUZZER
# works over the USB link itself, without needing the printer's own
# network/Wi-Fi up.
# Idempotent; "stop" unbinds the gadget from its UDC.
set -e
G=/sys/kernel/config/usb_gadget/c5tunnel
modprobe libcomposite 2>/dev/null || true
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
if [ "$1" = stop ]; then
    [ -f $G/UDC ] && echo "" > $G/UDC
    exit 0
fi
if [ -n "$(cat $G/UDC 2>/dev/null)" ]; then
    exit 0
fi
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
# Drop functions left by an older layout (e.g. acm.*): they would exceed
# the controller's endpoints and make the bind fail.
for f in configs/c.1/*.*; do
    [ -L "$f" ] || continue
    case "${f##*/}" in
        gser.usb[0-4]) ;;
        *) rm "$f"; rmdir "functions/${f##*/}" 2>/dev/null || true ;;
    esac
done
# Create and link in order so usb<n> gets ttyGS<n> and interface n.
for i in 0 1 2 3 4; do
    mkdir -p functions/gser.usb$i
    [ -e configs/c.1/gser.usb$i ] || ln -s functions/gser.usb$i configs/c.1/
done
udc=$(ls /sys/class/udc | head -n 1)
if [ -z "$udc" ]; then
    echo "no UDC in /sys/class/udc (USB peripheral mode not enabled)" >&2
    exit 1
fi
echo "$udc" > UDC
