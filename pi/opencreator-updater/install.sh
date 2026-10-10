#!/bin/sh
# Set up the OpenCreator Updater on a Pi tunnel install: a oneshot systemd
# service Moonraker's update_manager restarts whenever this repo is pulled,
# which merges pi/config/*.cfg's latest changes into printer_data/config
# (preserving user edits -- see update.py) and pulls Klipper.
#
# Run this (as the normal user, not root -- it calls sudo itself only for
# the two steps that need it) from the already-cloned ~/opencreator-tunnel
# checkout.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ASVC="$HOME/printer_data/moonraker.asvc"

sudo cp "$HERE/opencreator-updater.service" /etc/systemd/system/opencreator-updater.service
sudo systemctl daemon-reload

if [ -f "$ASVC" ] && ! grep -qx "opencreator-updater" "$ASVC"; then
    echo "opencreator-updater" >> "$ASVC"
fi

cat <<'EOF'
Service installed. Add this to moonraker.conf, then restart moonraker:

[update_manager opencreator-updater]
type: git_repo
path: ~/opencreator-tunnel
origin: https://github.com/FlashForge-C5-Modding-Group/opencreator-tunnel.git
primary_branch: main
managed_services: opencreator-updater

First run only seeds the merge-tracking baseline and does not change any
live config file; real auto-merging starts on the run after that one.
EOF
