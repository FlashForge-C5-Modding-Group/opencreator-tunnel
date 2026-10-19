#!/bin/sh
# Add OpenCreator to an existing, single-instance KIAUH installation.
# This deliberately does not reinstall Klipper, Moonraker, Fluidd, or nginx.
set -eu

usage() {
    cat <<'EOF'
Usage: sh pi/install-existing-kiauh.sh [--check] [--adopt-config] [--restart]

  --check         Detect the existing stack and report what would be needed.
  --adopt-config  Back up and replace a non-OpenCreator printer.cfg.
  --restart       Restart Moonraker and Klipper after installation (when idle).

Run as the normal KIAUH user from a cloned opencreator-tunnel checkout.
This installer requires the OpenCreator Kalico fork in the existing Klipper
checkout. It will not silently replace an upstream Klipper installation.
EOF
}

CHECK=0
ADOPT=0
RESTART=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --adopt-config) ADOPT=1 ;;
        --restart) RESTART=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

fail() { echo "opencreator-kiauh: $*" >&2; exit 1; }
say() { printf 'opencreator-kiauh: %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || fail "run as the normal KIAUH user, not root"
[ "$(uname -s)" = Linux ] || fail "this installer requires Linux"
for cmd in systemctl sudo git find cp mkdir grep ln readlink mktemp; do
    command -v "$cmd" >/dev/null 2>&1 || fail "missing command: $cmd"
done

PI=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO=$(CDPATH= cd -- "$PI/.." && pwd -P)
[ -d "$REPO/.git" ] || fail "use a cloned opencreator-tunnel checkout"

KLIPPER=${OC_KLIPPER_DIR:-$HOME/klipper}
KLIPPY_ENV=${OC_KLIPPY_ENV:-$HOME/klippy-env}
MOONRAKER=${OC_MOONRAKER_DIR:-$HOME/moonraker}
FLUIDD=${OC_FLUIDD_DIR:-$HOME/fluidd}
DATA=${OC_DATA_DIR:-$HOME/printer_data}
CONFIG=$DATA/config
TIMELAPSE=${OC_TIMELAPSE_DIR:-$HOME/moonraker-timelapse}
TIMELAPSE_REPO=${TIMELAPSE_REPO:-https://github.com/mainsail-crew/moonraker-timelapse.git}

[ -f "$KLIPPER/klippy/klippy.py" ] || fail "Klipper not found at $KLIPPER"
[ -x "$KLIPPY_ENV/bin/python" ] || fail "Klipper virtualenv not found at $KLIPPY_ENV"
[ -f "$MOONRAKER/moonraker/moonraker.py" ] || fail "Moonraker not found at $MOONRAKER"
[ -f "$FLUIDD/index.html" ] || fail "Fluidd not found at $FLUIDD"
[ -f "$CONFIG/moonraker.conf" ] || fail "Moonraker config not found at $CONFIG/moonraker.conf"
systemctl cat klipper.service >/dev/null 2>&1 ||
    fail "klipper.service not found (multi-instance KIAUH setups need manual adaptation)"
systemctl cat moonraker.service >/dev/null 2>&1 ||
    fail "moonraker.service not found (multi-instance KIAUH setups need manual adaptation)"

# Stock Klipper cannot parse the Creator 5 configuration or run its tool and
# AFC integration. Require a compatible existing KIAUH checkout, not merely
# a directory named ~/klipper. KIAUH can install the custom repository.
[ -f "$KLIPPER/klippy/extras/creator5_toolchanger.py" ] &&
[ -f "$KLIPPER/klippy/extras/creator5_remote_beeper.py" ] &&
[ -f "$KLIPPER/src/c5_eboard.c" ] ||
    fail "existing Klipper is not OpenCreator Kalico. In KIAUH select https://github.com/FlashForge-C5-Modding-Group/kalico.git branch something, then rerun this script"

component=$MOONRAKER/moonraker/components/opencreator_3mf.py
source=$PI/moonraker/opencreator_3mf.py
[ -f "$source" ] || fail "missing OpenCreator Moonraker component: $source"
if [ -e "$component" ] || [ -L "$component" ]; then
    [ "$(readlink -f "$component")" = "$(readlink -f "$source")" ] ||
        fail "$component already exists and points elsewhere; resolve it manually"
fi
timelapse_component=$MOONRAKER/moonraker/components/timelapse.py
if [ -e "$timelapse_component" ] || [ -L "$timelapse_component" ]; then
    [ -e "$timelapse_component" ] ||
        fail "$timelapse_component is a broken symlink; resolve it manually"
fi
if [ -e "$CONFIG/timelapse.cfg" ] || [ -L "$CONFIG/timelapse.cfg" ]; then
    [ -e "$CONFIG/timelapse.cfg" ] ||
        fail "$CONFIG/timelapse.cfg is a broken symlink; resolve it manually"
fi

if [ -L "$CONFIG/printer.cfg" ]; then
    fail "$CONFIG/printer.cfg is a symlink; resolve its target manually before installation"
fi
needs_adopt=0
if [ -e "$CONFIG/printer.cfg" ] &&
   ! grep -q '^\[include printer.base.cfg\]$' "$CONFIG/printer.cfg"; then
    needs_adopt=1
fi

say "found Klipper: $KLIPPER (OpenCreator Kalico modules present)"
say "found Moonraker: $MOONRAKER"
say "found Fluidd: $FLUIDD"
say "printer data: $DATA"
if [ ! -e "$CONFIG/timelapse.cfg" ]; then
    say "timelapse macros missing; they will be installed for printer.cfg"
fi
if [ "$needs_adopt" -eq 1 ]; then
    say "existing printer.cfg is not OpenCreator; --adopt-config is required to back it up and replace it"
fi
if [ "$CHECK" -eq 1 ]; then
    say "check only; no files or services changed"
    exit 0
fi
[ "$needs_adopt" -eq 0 ] || [ "$ADOPT" -eq 1 ] ||
    fail "rerun with --adopt-config after reviewing your existing printer.cfg"

mkdir -p "$CONFIG" "$DATA/logs" "$DATA/comms" "$DATA/gcodes"

copy_missing() {
    src=$1 dest=$2
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        return
    fi
    mkdir -p "$(dirname -- "$dest")"
    cp -p "$src" "$dest"
    say "added $dest"
}

# Kalico-specific config wins over the generic template on first install.
for name in printer.motor.cfg printer.kalico_mpc.cfg; do
    copy_missing "$PI/kalico/$name" "$CONFIG/$name"
done
find "$PI/config" -type f | while IFS= read -r src; do
    rel=${src#"$PI/config/"}
    [ "$rel" = printer.cfg ] && continue
    copy_missing "$src" "$CONFIG/$rel"
done

if [ ! -e "$CONFIG/printer.cfg" ]; then
    cp -p "$PI/config/printer.cfg" "$CONFIG/printer.cfg"
    say "added $CONFIG/printer.cfg"
elif [ "$needs_adopt" -eq 1 ]; then
    backup=$(mktemp "$CONFIG/printer.cfg.pre-opencreator.XXXXXX")
    cp -p "$CONFIG/printer.cfg" "$backup"
    cp -p "$PI/config/printer.cfg" "$CONFIG/printer.cfg"
    say "replaced printer.cfg; previous config saved at $backup"
else
    say "kept existing OpenCreator printer.cfg"
fi

# AFC PREP expects this JSON file to exist before Klipper starts.
if [ ! -s "$CONFIG/AFC/AFC.var.unit" ]; then
    printf '{}\n' > "$CONFIG/AFC/AFC.var.unit"
    say "initialized AFC.var.unit"
fi

# Keep KIAUH's Moonraker checkout and service. Add only the OpenCreator
# component, plus the updater registration in the existing config.
if [ -e "$component" ] || [ -L "$component" ]; then
    say "kept existing sliced-3MF Moonraker component"
else
    ln -s "$source" "$component"
    say "linked sliced-3MF Moonraker component"
fi

if [ ! -f "$CONFIG/moonraker.conf.pre-opencreator" ]; then
    cp -p "$CONFIG/moonraker.conf" "$CONFIG/moonraker.conf.pre-opencreator"
    say "backed up moonraker.conf"
fi
if ! grep -q '^\[opencreator_3mf\]$' "$CONFIG/moonraker.conf"; then
    printf '\n[opencreator_3mf]\n' >> "$CONFIG/moonraker.conf"
    say "enabled sliced-3MF uploads"
fi

# The Creator 5 printer.cfg includes timelapse*.cfg. Reuse any KIAUH-provided
# timelapse install; otherwise install the missing macro and component.
if [ ! -e "$CONFIG/timelapse.cfg" ] || [ ! -e "$timelapse_component" ]; then
    if [ ! -f "$TIMELAPSE/klipper_macro/timelapse.cfg" ] ||
       [ ! -f "$TIMELAPSE/component/timelapse.py" ]; then
        [ ! -e "$TIMELAPSE" ] ||
            fail "$TIMELAPSE exists but is not a complete moonraker-timelapse checkout"
        git clone --depth 1 "$TIMELAPSE_REPO" "$TIMELAPSE"
    fi
    if [ ! -e "$CONFIG/timelapse.cfg" ]; then
        ln -s "$TIMELAPSE/klipper_macro/timelapse.cfg" "$CONFIG/timelapse.cfg"
        say "linked timelapse macros"
    fi
    if [ ! -e "$timelapse_component" ]; then
        ln -s "$TIMELAPSE/component/timelapse.py" "$timelapse_component"
        say "linked timelapse Moonraker component"
    fi
fi
if ! grep -q '^\[timelapse\]$' "$CONFIG/moonraker.conf"; then
    printf '\n[timelapse]\noutput_path: %s/timelapse/\nframe_path: /tmp/timelapse/\nffmpeg_binary_path: /usr/bin/ffmpeg\n' \
        "$DATA" >> "$CONFIG/moonraker.conf"
    say "enabled timelapse in Moonraker"
fi
OC_REPO_DIR=$REPO OC_DATA=$DATA sh "$PI/opencreator-updater/install.sh"

# setup-gadget.sh installs the gser service and a dependency drop-in for the
# existing KIAUH klipper.service; it does not replace that service unit.
sudo sh "$PI/setup-gadget.sh"

if [ "$RESTART" -eq 1 ]; then
    sudo systemctl restart moonraker.service
    if [ -n "$(ls /sys/class/udc 2>/dev/null)" ]; then
        sudo systemctl restart klipper.service
    else
        say "USB peripheral controller not active yet; reboot before starting Klipper"
    fi
else
    say "services not restarted; when idle, restart Moonraker and Klipper"
fi
say "installation complete; reboot if setup-gadget.sh reported no UDC"
