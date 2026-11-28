
#!/bin/sh
# Enable USB peripheral mode (Raspberry Pi, Armbian on RK3328 and Orange Pi Zero 3W)
# and install the c5-tunnel gadget service plus the klipper.service drop-in.
# Run with sudo from pi/.
set -e
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")"

ensure_line() {  # ensure_line FILE LINE
    grep -qxF "$2" "$1" 2>/dev/null || echo "$2" >> "$1"
}

BOARD=other

if grep -qa "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
    BOARD=raspberry-pi
    CONFIG=/boot/firmware/config.txt
    [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
    OVERLAY=dtoverlay=dwc2,dr_mode=peripheral

    if ! awk -v want="$OVERLAY" '/^\[/ { s = $0 }
            $0 == want && (s == "" || s == "[all]") { found = 1 }
            END { exit !found }' "$CONFIG"; then
        printf '\n[all]\n%s\n' "$OVERLAY" >> "$CONFIG"
    fi

    ensure_line /etc/modules dwc2
    echo "configured dwc2 peripheral mode in $CONFIG"

elif [ -f /boot/armbianEnv.txt ] &&
        tr '\0' '\n' < /proc/device-tree/compatible 2>/dev/null |
        grep -qx 'rockchip,rk3328'; then
    BOARD=rk3328
    ENV=/boot/armbianEnv.txt

    command -v dtc >/dev/null || apt-get install -y device-tree-compiler
    install -d /boot/overlay-user

    names=
    for dts in overlays/c5-usb-otg-*.dts; do
        name=$(basename "$dts" .dts)
        dtc -@ -q -I dts -O dtb -o "/boot/overlay-user/$name.dtbo" "$dts"
        names="$names $name"
    done

    current=$(sed -n 's/^user_overlays=//p' "$ENV" | tail -n 1)
    wanted=$current

    for name in $names; do
        case " $wanted " in
            *" $name "*) ;;
            *) wanted="${wanted:+$wanted }$name" ;;
        esac
    done

    if [ "$wanted" != "$current" ]; then
        sed -i '/^user_overlays=/d' "$ENV"
        echo "user_overlays=$wanted" >> "$ENV"
    fi

    echo "configured the RK3328 OTG port for peripheral mode in $ENV"

elif [ -f /boot/armbianEnv.txt ] &&
        tr '\0' '\n' < /proc/device-tree/compatible 2>/dev/null |
        grep -qx 'xunlong,orangepi-zero3w'; then
    BOARD=orangepi-zero3w
    ENV=/boot/armbianEnv.txt
    NODE=/soc@3000000/usbc2@12/xhci2-controller@6a00000

    FDTFILE=$(sed -n 's/^[[:space:]]*fdtfile=[[:space:]]*//p' "$ENV" | tail -n 1)
    [ -n "$FDTFILE" ] || {
        echo "error: fdtfile is missing from $ENV" >&2
        exit 1
    }

    case "$FDTFILE" in
        /*) DTB=$FDTFILE ;;
        *)  DTB="/boot/dtb/$FDTFILE" ;;
    esac

    [ -f "$DTB" ] || {
        echo "error: device tree not found: $DTB" >&2
        exit 1
    }

    if ! command -v fdtget >/dev/null || ! command -v fdtput >/dev/null; then
        apt-get install -y device-tree-compiler
    fi

    command -v fdtget >/dev/null && command -v fdtput >/dev/null || {
        echo "error: fdtget/fdtput are not available" >&2
        exit 1
    }

    fdtget -p "$DTB" "$NODE" >/dev/null 2>&1 || {
        echo "error: USB controller node not found in $DTB" >&2
        exit 1
    }

    current_mode=$(fdtget -t s "$DTB" "$NODE" role-switch-default-mode 2>/dev/null || true)

    if [ "$current_mode" != "device" ]; then
        BACKUP="${DTB}.pre-c5-tunnel"

        if [ -e "$BACKUP" ]; then
            n=1
            while [ -e "${BACKUP}.${n}" ]; do
                n=$((n + 1))
            done
            BACKUP="${BACKUP}.${n}"
        fi

        cp -p "$DTB" "$BACKUP"
        echo "created device tree backup: $BACKUP"

        fdtput -t s "$DTB" "$NODE" role-switch-default-mode device

        verified=$(fdtget -t s "$DTB" "$NODE" role-switch-default-mode)
        [ "$verified" = "device" ] || {
            echo "error: failed to verify USB device mode in $DTB" >&2
            exit 1
        }
    fi

    echo "configured Orange Pi Zero 3W USB controller for device mode in $DTB"

else
    echo "no board-specific USB peripheral configuration available."
    echo "Ensure the USB device controller is configured manually."
fi

ensure_line /etc/modules libcomposite

# Prepare the gadget script.
GADGET_SOURCE=c5-tunnel-gadget.sh
GADGET_INSTALL=/usr/local/sbin/c5-tunnel-gadget.sh

if [ "$BOARD" = "orangepi-zero3w" ]; then
    # Patch a temporary copy, leaving the repository source unchanged.
    GADGET_TMP=$(mktemp)
    trap 'rm -f "$GADGET_TMP"' EXIT HUP INT TERM

    cp "$GADGET_SOURCE" "$GADGET_TMP"

    # Select the known-working UDC on Orange Pi Zero 3W.
    sed -i '/^if \[ -n "\$C5_TUNNEL_UDC" \]; then$/,/^fi$/c\
if tr "\\000" "\\n" < /proc/device-tree/compatible 2>/dev/null |\
        grep -qx "xunlong,orangepi-zero3w"; then\
    UDCS=6a00000.xhci2-controller\
elif [ -n "$C5_TUNNEL_UDC" ]; then\
    UDCS=$C5_TUNNEL_UDC\
elif [ -r "$UDC_FILE" ]; then\
    UDCS=$(cat "$UDC_FILE")\
else\
    UDCS=$(ls /sys/class/udc 2>/dev/null || true)\
fi' "$GADGET_TMP"

    install -m 755 "$GADGET_TMP" "$GADGET_INSTALL"
    rm -f "$GADGET_TMP"
    trap - EXIT HUP INT TERM
    echo "installed gadget with Orange Pi Zero 3W UDC selection"

else
    install -m 755 "$GADGET_SOURCE" "$GADGET_INSTALL"
    echo "installed standard gadget script"
fi

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
