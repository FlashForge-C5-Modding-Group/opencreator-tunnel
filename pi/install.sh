#!/bin/sh
# OpenCreator tunnel SBC install: Fluidd, Moonraker, our Kalico, the C5
# config and the tunnel gadget service. Everything is downloaded here; see
# README.md ("SBC").
set -eu

SKIP_CYTHON=${SKIP_CYTHON:-0}
VERBOSE=${VERBOSE:-0}
NUMPY_BUILD_JOBS=${NUMPY_BUILD_JOBS:-2}
MODEL=${OC_MODEL:-}
next_model=0
for option in "$@"; do
    if [ "$next_model" -eq 1 ]; then MODEL=$option; next_model=0; continue; fi
    case $option in
        --model) next_model=1 ;;
        --model=*) MODEL=${option#--model=} ;;
        --skip-cython) SKIP_CYTHON=1 ;;
        -v|--verbose) VERBOSE=1 ;;
        -h|--help)
            echo "Usage: sh pi/install.sh [--model creator5|creator5pro] [--skip-cython] [--verbose]"
            echo "  --model        The printer this Pi is connected to (asked if not given)."
            echo "  --skip-cython  Skip compiling Cython itself; use its Python implementation."
            echo "  -v, --verbose Enable detailed pip and build output."
            exit 0 ;;
        *) echo "Unknown option: $option" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not root" >&2; exit 1; }

# Which printer the Pi drives: the Creator 5 has no chamber heater, chamber
# fans or aux fan, so only a Creator 5 Pro gets printer.pro.cfg (through
# printer.model.cfg, see printer.base.cfg).
ask_model() {
    case ${MODEL:-} in
        creator5|creator5pro|'') ;;
        *) echo "--model must be creator5 or creator5pro" >&2; exit 2 ;;
    esac
    while [ -z "${MODEL:-}" ]; do
        if ! (: </dev/tty) 2>/dev/null; then
            echo "No terminal to ask on: pass --model creator5 or --model creator5pro" >&2
            exit 2
        fi
        printf '\nWhich printer is this Pi connected to?\n  1) Creator 5\n  2) Creator 5 Pro\nChoose 1 or 2: ' >/dev/tty
        read -r answer </dev/tty || exit 2
        case $answer in
            1) MODEL=creator5 ;;
            2) MODEL=creator5pro ;;
        esac
    done
}
write_model() {  # write_model CONFIG_DIR
    if [ "$MODEL" = creator5pro ]; then
        printf '# Written by the OpenCreator installer: Creator 5 Pro\n[include printer.pro.cfg]\n'
    else
        printf '# Written by the OpenCreator installer: Creator 5 (no chamber heater, chamber fans or aux fan)\n'
    fi > "$1/printer.model.cfg"
}
ask_model

# This environment option also reaches pip's isolated build environments.
case $SKIP_CYTHON in
    0) ;;
    1)
        export NO_CYTHON_COMPILE=true
        echo "Skipping Cython compilation (Python implementation remains available)" ;;
    *) echo "SKIP_CYTHON must be 0 or 1" >&2; exit 2 ;;
esac
case $VERBOSE in
    0) ;;
    1)
        case ${PIP_VERBOSE:-0} in 0) PIP_VERBOSE=1 ;; esac
        export PIP_VERBOSE
        export PYTHONUNBUFFERED=1
        echo "Verbose pip and build output enabled" ;;
    *) echo "VERBOSE must be 0 or 1" >&2; exit 2 ;;
esac
case $NUMPY_BUILD_JOBS in
    ''|*[!0-9]*) echo "NUMPY_BUILD_JOBS must be a positive integer" >&2; exit 2 ;;
esac
[ "$NUMPY_BUILD_JOBS" -gt 0 ] || {
    echo "NUMPY_BUILD_JOBS must be a positive integer" >&2; exit 2
}

# SBC images often mount /tmp as a small RAM filesystem. Use the SD card
# for unpacked source, build dependencies and compiler temporary files.
TMPDIR=${TMPDIR:-$HOME/.cache/opencreator/build-tmp}
mkdir -p "$TMPDIR"
export TMPDIR

TUNNEL_REPO=${TUNNEL_REPO:-https://github.com/FlashForge-C5-Modding-Group/opencreator-tunnel.git}
TUNNEL_BRANCH=${TUNNEL_BRANCH:-main}
KALICO_REPO=${KALICO_REPO:-https://github.com/FlashForge-C5-Modding-Group/kalico.git}
KALICO_BRANCH=${KALICO_BRANCH:-something}
MOONRAKER_REPO=${MOONRAKER_REPO:-https://github.com/Arksine/moonraker.git}
FLUIDD_ZIP=${FLUIDD_ZIP:-https://github.com/fluidd-core/fluidd/releases/latest/download/fluidd.zip}
FLUIDD_CONFIG_REPO=${FLUIDD_CONFIG_REPO:-https://github.com/fluidd-core/fluidd-config.git}

OC_USER=$(whoami)
TUNNEL_DIR=${TUNNEL_DIR:-$HOME/opencreator-tunnel}
KLIPPER_DIR=$HOME/klipper
KLIPPY_ENV=$HOME/klippy-env
MOONRAKER_DIR=$HOME/moonraker
FLUIDD_DIR=$HOME/fluidd
FLUIDD_CONFIG_DIR=$HOME/fluidd-config
DATA=$HOME/printer_data
CONFIG=$DATA/config

step() {
    printf '\n==> %s\n' "$*"
}

# clone_or_update DIR REPO BRANCH
clone_or_update() {
    if [ -d "$1/.git" ]; then
        git -C "$1" fetch --quiet origin "$3"
        git -C "$1" checkout --quiet "$3"
        git -C "$1" merge --ff-only --quiet "origin/$3" ||
            echo "$1: local changes, left as-is"
    else
        git clone --branch "$3" "$2" "$1"
    fi
}

step "System packages"
sudo apt-get update
sudo apt-get install --yes git curl unzip nginx python3 python3-venv \
    python3-dev libffi-dev build-essential libncurses-dev libusb-dev \
    pkg-config ffmpeg libopenblas-dev

step "opencreator-tunnel ($TUNNEL_BRANCH)"
clone_or_update "$TUNNEL_DIR" "$TUNNEL_REPO" "$TUNNEL_BRANCH"
PI=$TUNNEL_DIR/pi

mkdir -p "$CONFIG" "$DATA/logs" "$DATA/comms" "$DATA/gcodes" "$DATA/systemd"

step "Fluidd"
# An installed Fluidd is left to Moonraker's update manager, so files added
# to it stay; only a missing one is downloaded.
if [ -f "$FLUIDD_DIR/index.html" ]; then
    echo "Fluidd already installed, left as-is"
else
    tmp=$(mktemp -d)
    curl -fL -o "$tmp/fluidd.zip" "$FLUIDD_ZIP"
    mkdir -p "$FLUIDD_DIR"
    unzip -q -o "$tmp/fluidd.zip" -d "$FLUIDD_DIR"
    rm -rf "$tmp"
fi
clone_or_update "$FLUIDD_CONFIG_DIR" "$FLUIDD_CONFIG_REPO" master
ln -sf "$FLUIDD_CONFIG_DIR/client.cfg" "$CONFIG/fluidd.cfg"

# nginx runs as www-data and needs to traverse $HOME to reach Fluidd.
chmod o+x "$HOME"
sudo tee /etc/nginx/sites-available/fluidd >/dev/null <<EOF
upstream moonraker {
    server 127.0.0.1:7125;
}

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    root $FLUIDD_DIR;
    index index.html;
    client_max_body_size 0;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    location = /index.html {
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }

    location /websocket {
        proxy_pass http://moonraker/websocket;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 86400;
    }

    location ~ ^/(printer|api|access|machine|server)/ {
        proxy_pass http://moonraker\$request_uri;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Scheme \$scheme;
    }
}
EOF
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/fluidd /etc/nginx/sites-enabled/fluidd
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx

step "Moonraker"
clone_or_update "$MOONRAKER_DIR" "$MOONRAKER_REPO" master
if [ ! -f "$CONFIG/moonraker.conf" ]; then
    cat > "$CONFIG/moonraker.conf" <<EOF
[server]
host: 0.0.0.0
port: 7125
klippy_uds_address: $DATA/comms/klippy.sock

[authorization]
trusted_clients:
    10.0.0.0/8
    127.0.0.0/8
    169.254.0.0/16
    172.16.0.0/12
    192.168.0.0/16
    FE80::/10
    ::1/128
cors_domains:
    *.lan
    *.local
    *://localhost
    *://localhost:*
    *://app.fluidd.xyz

[octoprint_compat]

[history]

[file_manager]
enable_object_processing: True

[update_manager]
refresh_interval: 168

[update_manager fluidd]
type: web
channel: stable
repo: fluidd-core/fluidd
path: ~/fluidd

[update_manager fluidd-config]
type: git_repo
primary_branch: master
path: ~/fluidd-config
origin: $FLUIDD_CONFIG_REPO
managed_services: klipper

# Phone/chat notifications through Apprise. Uncomment and put in your
# service's URL (Discord, Telegram, ntfy, Pushover, ...):
# https://github.com/caronc/apprise/wiki
#[notifier print_events]
#url: tgram://BOT_TOKEN/CHAT_ID
#events: complete, error, cancelled, paused
#title: OpenCreator
#body: Print {event_name}: {event_args[1].filename}
# Door open during a print (needs door notify on: C5_MISC_DOOR_NOTIFY_ON)
#[notifier door_open]
#url: tgram://BOT_TOKEN/CHAT_ID
#events: gcode
#title: OpenCreator
#body: {event_message}

# Spool tracking with a Spoolman server. Uncomment and set its address.
#[spoolman]
#server: http://192.168.1.100:7912
#sync_rate: 5
EOF
fi
"$MOONRAKER_DIR/scripts/install-moonraker.sh" -f -d "$DATA"
ln -sf "$PI/moonraker/opencreator_3mf.py" \
    "$MOONRAKER_DIR/moonraker/components/opencreator_3mf.py"
grep -q '^\[opencreator_3mf\]' "$CONFIG/moonraker.conf" ||
    printf '\n[opencreator_3mf]\n' >> "$CONFIG/moonraker.conf"

step "Timelapse"
TIMELAPSE_REPO=${TIMELAPSE_REPO:-https://github.com/mainsail-crew/moonraker-timelapse.git}
clone_or_update "$HOME/moonraker-timelapse" "$TIMELAPSE_REPO" main
ln -sf "$HOME/moonraker-timelapse/component/timelapse.py" \
    "$MOONRAKER_DIR/moonraker/components/timelapse.py"
ln -sf "$HOME/moonraker-timelapse/klipper_macro/timelapse.cfg" "$CONFIG/timelapse.cfg"
# Frames come from the webcam set up in Fluidd.
grep -q '^\[timelapse\]' "$CONFIG/moonraker.conf" ||
    printf '\n[timelapse]\noutput_path: %s/timelapse/\nframe_path: /tmp/timelapse/\nffmpeg_binary_path: /usr/bin/ffmpeg\n' \
        "$DATA" >> "$CONFIG/moonraker.conf"
grep -q '^\[update_manager timelapse\]' "$CONFIG/moonraker.conf" ||
    printf '\n[update_manager timelapse]\ntype: git_repo\nprimary_branch: main\npath: %s/moonraker-timelapse\norigin: %s\nmanaged_services: klipper moonraker\n' \
        "$HOME" "$TIMELAPSE_REPO" >> "$CONFIG/moonraker.conf"

step "Kalico ($KALICO_BRANCH)"
clone_or_update "$KLIPPER_DIR" "$KALICO_REPO" "$KALICO_BRANCH"
[ -d "$KLIPPY_ENV" ] || python3 -m venv "$KLIPPY_ENV"
"$KLIPPY_ENV/bin/pip" install --upgrade pip
# Limit source-build workers to fit small SBCs. Use packaged OpenBLAS instead
# of compiling the large bundled LAPACK fallback that crashed GCC 14 on ARM32.
numpy_build_settings=" --config-settings=compile-args=-j$NUMPY_BUILD_JOBS"
numpy_build_settings="$numpy_build_settings --config-settings=setup-args=-Dblas=openblas --config-settings=setup-args=-Dlapack=openblas"
case $(uname -m) in
    armv*l)
        # Avoid the optional NEON dispatch variants that also crashed GCC 14.
        numpy_build_settings="$numpy_build_settings --config-settings=setup-args=-Dcpu-dispatch=none"
        ;;
esac
klippy_requirements=$(mktemp)
trap 'rm -f "$klippy_requirements"' 0
# pip applies build settings to packages from -r only when set per requirement.
sed "/^numpy[=<>!~]/ s/$/$numpy_build_settings/" \
    "$KLIPPER_DIR/scripts/klippy-requirements.txt" > "$klippy_requirements"
"$KLIPPY_ENV/bin/pip" install -r "$klippy_requirements"
rm -f "$klippy_requirements"
trap - 0
cat > "$DATA/systemd/klipper.env" <<EOF
KLIPPER_ARGS="$KLIPPER_DIR/klippy/klippy.py $CONFIG/printer.cfg -I $DATA/comms/klippy.serial -l $DATA/logs/klippy.log -a $DATA/comms/klippy.sock"
EOF
sudo tee /etc/systemd/system/klipper.service >/dev/null <<EOF
[Unit]
Description=Klipper 3D Printer Firmware SV1
Documentation=https://www.klipper3d.org/
After=network-online.target
Wants=udev.target

[Install]
WantedBy=multi-user.target

[Service]
Type=simple
User=$OC_USER
RemainAfterExit=yes
WorkingDirectory=$KLIPPER_DIR
EnvironmentFile=$DATA/systemd/klipper.env
ExecStart=$KLIPPY_ENV/bin/python \$KLIPPER_ARGS
Restart=always
RestartSec=10
EOF
sudo systemctl daemon-reload
sudo systemctl enable klipper.service

step "Printer config"
# Files the config doesn't have yet are added; every existing file is kept
# (the OpenCreator updater merges template changes into them).
added=0
kept=0
copy_missing() {  # copy_missing SRC_FILE DEST_FILE
    if [ -e "$2" ]; then
        kept=$((kept + 1))
    else
        mkdir -p "$(dirname "$2")"
        cp -p "$1" "$2"
        added=$((added + 1))
    fi
}
# Kalico's MPC file goes first.
for f in printer.kalico_mpc.cfg; do
    copy_missing "$PI/kalico/$f" "$CONFIG/$f"
done
for src in $(cd "$PI/config" && find . -type f); do
    copy_missing "$PI/config/$src" "$CONFIG/$src"
done
echo "Config: $added files added, $kept existing files kept"
write_model "$CONFIG"
echo "Printer: $MODEL (printer.model.cfg)"
# The include goes first: the end of printer.cfg is Klipper's SAVE_CONFIG
# block. A kept printer.cfg lacks it, or has it lower down.
if [ "$(head -n 1 "$CONFIG/printer.cfg")" != '[include timelapse*.cfg]' ]; then
    { echo '[include timelapse*.cfg]'; grep -v '^\[include timelapse\*\.cfg\]$' "$CONFIG/printer.cfg"; } \
        > "$CONFIG/printer.cfg.new" && mv "$CONFIG/printer.cfg.new" "$CONFIG/printer.cfg"
fi
# AFC stops with an error while its variables file is missing or empty.
if [ ! -s "$CONFIG/AFC/AFC.var.unit" ]; then
    mkdir -p "$CONFIG/AFC"
    echo '{}' > "$CONFIG/AFC/AFC.var.unit"
fi

step "OpenCreator updater"
OC_REPO_DIR=$TUNNEL_DIR OC_DATA=$DATA sh "$PI/opencreator-updater/install.sh"

step "Tunnel gadget service"
sudo sh "$PI/setup-gadget.sh"

step "Starting"
sudo systemctl restart moonraker

# OpenCreator accent color for the Fluidd theme in .fluidd-theme, unless
# the user already picked a color other than Fluidd's default.
api=http://127.0.0.1:7125/server/database/item
i=0
until curl -fs http://127.0.0.1:7125/server/info >/dev/null 2>&1 || [ $i -ge 30 ]; do
    sleep 1; i=$((i + 1))
done
color=$(curl -fs "$api?namespace=fluidd&key=uiSettings.theme.color" 2>/dev/null || true)
case $color in
*'"#'[0-9A-Fa-f]*) echo "$color" | grep -qi '#2196F3' || color=keep ;;
esac
[ "$color" = keep ] ||
    curl -fs -X POST -H 'Content-Type: application/json' \
        -d '{"namespace":"fluidd","key":"uiSettings.theme.color","value":"#FF6A2B"}' \
        "$api" >/dev/null || true
# OpenCreator macro categories for Fluidd, unless the user already has a layout.
curl -fs "$api?namespace=fluidd&key=macros" >/dev/null 2>&1 ||
    { printf '{"namespace":"fluidd","key":"macros","value":'; cat "$PI/fluidd/macros.json"; printf '}'; } |
    curl -fs -X POST -H 'Content-Type: application/json' -d @- "$api" >/dev/null || true
if [ -n "$(ls /sys/class/udc 2>/dev/null)" ]; then
    sudo systemctl restart klipper
    echo "Done. Fluidd: http://$(hostname -I | cut -d' ' -f1)/"
else
    echo "Done. Reboot to enable the USB gadget; Klipper starts after it."
fi
