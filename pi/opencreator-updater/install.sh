#!/bin/sh
# Set up the OpenCreator Updater for this checkout, wherever it lives:
# opencreator-updater.service, the moonraker.asvc entry and the
# [update_manager opencreator-updater] section in moonraker.conf (added,
# or corrected in place). Safe to rerun; see README.md ("OpenCreator
# Updater"). Prints one "opencreator-updater: ..." line per change.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd -P)
OC_USER=${SUDO_USER:-$(id -un)}
if [ -n "${SUDO_USER:-}" ]; then OC_HOME=$(eval echo "~$SUDO_USER"); else OC_HOME=$HOME; fi
REPO_DIR=${OC_REPO_DIR:-$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null ||
    (cd "$HERE/../.." && pwd -P))}
DATA=${OC_DATA:-$OC_HOME/printer_data}
CONF=$DATA/config/moonraker.conf
ASVC=$DATA/moonraker.asvc
ORIGIN=${TUNNEL_REPO:-https://github.com/FlashForge-C5-Modding-Group/opencreator-tunnel.git}
BRANCH=${TUNNEL_BRANCH:-main}
SECTION="update_manager opencreator-updater"
UNIT=/etc/systemd/system/opencreator-updater.service

say() { echo "opencreator-updater: $*"; }

git -C "$REPO_DIR" rev-parse --verify -q HEAD >/dev/null 2>&1 ||
    say "warning: $REPO_DIR is not a git checkout; Moonraker cannot update it (run repair.sh)"

# --- service
tmp=$(mktemp)
sed -e "s|__OC_USER__|$OC_USER|g" -e "s|__OC_DATA__|$DATA|g" \
    -e "s|__OC_REPO__|$REPO_DIR|g" "$HERE/opencreator-updater.service" >"$tmp"
if ! cmp -s "$tmp" "$UNIT" 2>/dev/null; then
    sudo cp "$tmp" "$UNIT"
    sudo systemctl daemon-reload
    say "installed $UNIT for $REPO_DIR"
fi
rm -f "$tmp"

# --- moonraker.asvc
if [ -f "$ASVC" ] && ! grep -qx opencreator-updater "$ASVC"; then
    echo opencreator-updater >>"$ASVC"
    say "added opencreator-updater to $ASVC"
fi

# --- moonraker.conf: path is accepted as absolute or ~/-relative
[ -f "$CONF" ] || { say "no $CONF; add the [$SECTION] section by hand"; exit 0; }
tilde=$REPO_DIR
case $REPO_DIR in "$OC_HOME"/*) tilde="~/${REPO_DIR#"$OC_HOME"/}" ;; esac

if ! grep -q "^\[$SECTION\]" "$CONF"; then
    printf '\n[%s]\ntype: git_repo\npath: %s\norigin: %s\nprimary_branch: %s\nmanaged_services: opencreator-updater\n' \
        "$SECTION" "$tilde" "$ORIGIN" "$BRANCH" >>"$CONF"
    say "added [$SECTION] to $CONF"
    exit 0
fi

tmp=$(mktemp)
awk -v sec="[$SECTION]" -v p="$tilde" -v pa="$REPO_DIR" -v o="$ORIGIN" -v b="$BRANCH" '
    function put(k, v) { print k ": " v; seen[k] = 1 }
    function flush() {
        if (!insec) return
        if (!seen["type"]) put("type", "git_repo")
        if (!seen["path"]) put("path", p)
        if (!seen["origin"]) put("origin", o)
        if (!seen["primary_branch"]) put("primary_branch", b)
        if (!seen["managed_services"]) put("managed_services", "opencreator-updater")
        insec = 0
    }
    /^\[/ { flush(); insec = ($0 == sec); skip = 0 }
    insec && skip && /^[ \t]+[^ \t]/ { next }
    insec { skip = 0 }
    insec && match($0, /^[a-z_]+[ \t]*[:=]/) {
        k = $0; sub(/[ \t]*[:=].*/, "", k)
        v = $0; sub(/^[^:=]*[:=][ \t]*/, "", v); sub(/[ \t]+$/, "", v)
        if (k in seen) { skip = 1; next }
        if (k == "path" && (v == p || v == pa)) { seen[k] = 1; print; next }
        if (k == "type") { put(k, "git_repo"); skip = 1; next }
        if (k == "path") { put(k, p); skip = 1; next }
        if (k == "origin") { put(k, o); skip = 1; next }
        if (k == "primary_branch") { put(k, b); skip = 1; next }
        seen[k] = 1
    }
    { print }
    END { flush() }' "$CONF" >"$tmp"
if ! cmp -s "$tmp" "$CONF"; then
    cp "$CONF" "$CONF.bak-updater"
    cat "$tmp" >"$CONF"
    say "corrected [$SECTION] in $CONF (backup: moonraker.conf.bak-updater)"
fi
rm -f "$tmp"
