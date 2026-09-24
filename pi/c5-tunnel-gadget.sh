#!/bin/sh
# Create a four-port CDC-ACM USB gadget (/dev/ttyGS0..3) through configfs.
# Port n appears on the printer as the ACM on USB interface 2n
# (1.0, 1.2, 1.4, 1.6), which c5_bridge.py maps to one MCU each.
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
# Create and link in order so usb<n> gets ttyGS<n> and interfaces 2n/2n+1.
for i in 0 1 2 3; do
    mkdir -p functions/acm.usb$i
    [ -e configs/c.1/acm.usb$i ] || ln -s functions/acm.usb$i configs/c.1/
done
udc=$(ls /sys/class/udc | head -n 1)
if [ -z "$udc" ]; then
    echo "no UDC in /sys/class/udc (USB peripheral mode not enabled)" >&2
    exit 1
fi
echo "$udc" > UDC
