#!/bin/sh
# Make Moonraker's [update_manager opencreator-updater] entry valid again:
# a git checkout of opencreator-tunnel at ~/opencreator-tunnel, owned by
# this user, on the right origin and branch, plus the moonraker.conf
# section and moonraker.asvc entry. Safe to rerun; see README.md
# ("Updater repair").
set -u

TUNNEL_REPO=${TUNNEL_REPO:-https://github.com/FlashForge-C5-Modding-Group/opencreator-tunnel.git}
TUNNEL_BRANCH=${TUNNEL_BRANCH:-main}
DATA=${OC_DATA:-$HOME/printer_data}
REPO_DIR=${OC_REPO_DIR:-$HOME/opencreator-tunnel}
CONF=$DATA/config/moonraker.conf
ASVC=$DATA/moonraker.asvc
MOONRAKER_API=${OC_MOONRAKER_API:-http://127.0.0.1:7125}
SECTION="update_manager opencreator-updater"
OC_USER=$(id -un)
CHANGED=0

say() { echo "opencreator-repair: $*"; }

[ "$(id -u)" -ne 0 ] || { say "run as your normal user, not root"; exit 1; }
command -v git >/dev/null || { say "git is not installed"; exit 1; }

# "https://host/owner/repo(.git)(/)" -> "host/owner/repo"
norm_url() {
    echo "$1" | sed -e 's#^[a-z+]*://##' -e 's#^git@\([^:]*\):#\1/#' \
        -e 's#/*$##' -e 's#\.git$##' | tr 'A-Z' 'a-z'
}

fresh_clone() {
    if [ -e "$REPO_DIR" ]; then
        aside="$REPO_DIR.broken-$(date +%Y%m%d-%H%M%S)"
        mv "$REPO_DIR" "$aside" || { say "cannot move $REPO_DIR aside"; exit 1; }
        say "moved the old $REPO_DIR to $aside"
    fi
    git clone --branch "$TUNNEL_BRANCH" "$TUNNEL_REPO" "$REPO_DIR" ||
        { say "clone failed (network?)"; exit 1; }
    CHANGED=1
}

# --- ownership: git refuses repos owned by another user ("dubious
# ownership"), which Moonraker reports as "No git repo detected"
if [ -e "$REPO_DIR" ] &&
    [ -n "$(find "$REPO_DIR" ! -user "$OC_USER" -print -quit 2>/dev/null)" ]; then
    if sudo -n chown -R "$OC_USER:$(id -gn)" "$REPO_DIR" 2>/dev/null; then
        say "gave $REPO_DIR back to $OC_USER"
        CHANGED=1
    else
        say "$REPO_DIR has files not owned by $OC_USER; run:"
        say "  sudo chown -R $OC_USER:$(id -gn) $REPO_DIR"
        exit 1
    fi
fi

# --- the checkout itself
top=""
[ -d "$REPO_DIR" ] && top=$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null)
if [ "$top" != "$(cd "$REPO_DIR" 2>/dev/null && pwd -P)" ] ||
    ! git -C "$REPO_DIR" rev-parse --verify -q HEAD >/dev/null; then
    say "$REPO_DIR is not a usable git checkout"
    fresh_clone
fi

origin=$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || echo "")
if [ "$(norm_url "$origin")" != "$(norm_url "$TUNNEL_REPO")" ]; then
    if [ -n "$origin" ]; then
        git -C "$REPO_DIR" remote set-url origin "$TUNNEL_REPO"
    else
        git -C "$REPO_DIR" remote add origin "$TUNNEL_REPO"
    fi
    say "origin set to $TUNNEL_REPO (was '${origin:-none}')"
    CHANGED=1
fi

if ! git -C "$REPO_DIR" fetch --quiet origin "$TUNNEL_BRANCH"; then
    say "fetching origin/$TUNNEL_BRANCH failed (network?); leaving the branch alone"
else
    branch=$(git -C "$REPO_DIR" symbolic-ref -q --short HEAD || echo "")
    upstream=$(git -C "$REPO_DIR" rev-parse --abbrev-ref -q "$TUNNEL_BRANCH@{upstream}" 2>/dev/null || echo "")
    if [ "$branch" != "$TUNNEL_BRANCH" ] || [ "$upstream" != "origin/$TUNNEL_BRANCH" ]; then
        if [ -n "$(git -C "$REPO_DIR" status --porcelain --untracked-files=no)" ]; then
            say "on '${branch:-detached HEAD}' with local changes; not switching to $TUNNEL_BRANCH"
            say "commit or discard them, or use Recover in the update manager"
        else
            git -C "$REPO_DIR" checkout --quiet -B "$TUNNEL_BRANCH" "origin/$TUNNEL_BRANCH"
            git -C "$REPO_DIR" branch --quiet -u "origin/$TUNNEL_BRANCH" "$TUNNEL_BRANCH"
            say "switched to $TUNNEL_BRANCH (was '${branch:-detached HEAD}')"
            CHANGED=1
        fi
    fi
fi

# --- moonraker.conf: add the section, or correct its four keys in place
if [ -f "$CONF" ]; then
    want_path="~/${REPO_DIR#"$HOME"/}"
    [ "${REPO_DIR#"$HOME"/}" = "$REPO_DIR" ] && want_path=$REPO_DIR
    if ! grep -q "^\[$SECTION\]" "$CONF"; then
        printf '\n[%s]\ntype: git_repo\npath: %s\norigin: %s\nprimary_branch: %s\nmanaged_services: opencreator-updater\n' \
            "$SECTION" "$want_path" "$TUNNEL_REPO" "$TUNNEL_BRANCH" >>"$CONF"
        say "added [$SECTION] to $CONF"
        CHANGED=1
    else
        tmp=$(mktemp)
        awk -v sec="[$SECTION]" -v p="$want_path" -v o="$TUNNEL_REPO" \
            -v b="$TUNNEL_BRANCH" '
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
            /^\[/ { flush(); insec = ($0 == sec) }
            insec && match($0, /^[a-z_]+[ \t]*[:=]/) {
                k = $0; sub(/[ \t]*[:=].*/, "", k)
                if (k == "type") { put(k, "git_repo"); next }
                if (k == "path") { put(k, p); next }
                if (k == "origin") { put(k, o); next }
                if (k == "primary_branch") { put(k, b); next }
            }
            { print }
            END { flush() }' "$CONF" >"$tmp"
        if ! cmp -s "$tmp" "$CONF"; then
            cp "$CONF" "$CONF.bak-repair"
            cat "$tmp" >"$CONF"
            say "corrected [$SECTION] in $CONF (backup: moonraker.conf.bak-repair)"
            CHANGED=1
        fi
        rm -f "$tmp"
    fi
else
    say "no $CONF; skipping the moonraker.conf section"
fi

if [ -f "$ASVC" ] && ! grep -qx opencreator-updater "$ASVC"; then
    echo opencreator-updater >>"$ASVC"
    say "added opencreator-updater to moonraker.asvc"
    CHANGED=1
fi

if [ ! -f /etc/systemd/system/opencreator-updater.service ]; then
    sh "$REPO_DIR/pi/opencreator-updater/install.sh" >/dev/null &&
        say "installed opencreator-updater.service"
    CHANGED=1
fi

if [ "$CHANGED" = 1 ]; then
    if curl -fsS --max-time 10 -X POST "$MOONRAKER_API/server/restart" >/dev/null 2>&1; then
        say "asked Moonraker to restart"
    else
        say "restart Moonraker to pick up the changes"
    fi
else
    say "nothing to fix"
fi
