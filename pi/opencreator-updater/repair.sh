#!/bin/sh
# Make Moonraker's [update_manager opencreator-updater] entry valid again:
# this checkout (or a copy without git) becomes a git checkout of
# opencreator-tunnel's main branch, owned by this user, then install.sh sets
# up the service, moonraker.asvc and moonraker.conf. Safe to rerun; see
# README.md ("Updater repair").
set -u

TUNNEL_REPO=${TUNNEL_REPO:-https://github.com/FlashForge-C5-Modding-Group/opencreator-tunnel.git}
TUNNEL_BRANCH=${TUNNEL_BRANCH:-main}
HERE=$(cd "$(dirname "$0")" && pwd -P)
OC_USER=$(id -un)
CHANGED=0

say() { echo "opencreator-repair: $*"; }
g() { git -C "$REPO_DIR" -c user.name=opencreator-repair \
    -c user.email=opencreator-repair@localhost "$@"; }

[ "$(id -u)" -ne 0 ] || { say "run as your normal user, not root"; exit 1; }
command -v git >/dev/null || { say "git is not installed"; exit 1; }

# The tree this script lives in (git or not), unless OC_REPO_DIR is set.
if [ -n "${OC_REPO_DIR:-}" ]; then
    REPO_DIR=$OC_REPO_DIR
else
    REPO_DIR=$(cd "$HERE/../.." && pwd -P)
fi

# "https://host/owner/repo(.git)(/)" -> "host/owner/repo"
norm_url() {
    echo "$1" | sed -e 's#^[a-z+]*://##' -e 's#^git@\([^:]*\):#\1/#' \
        -e 's#/*$##' -e 's#\.git$##' | tr 'A-Z' 'a-z'
}

# Local edits to tracked files are stashed, never thrown away.
stash_local() {
    [ -n "$(g status --porcelain --untracked-files=no)" ] || return 0
    g stash push -q -m "opencreator-repair $(date +%Y-%m-%d)" &&
        say "local changes stashed; 'git -C $REPO_DIR stash pop' brings them back"
}

if [ ! -d "$REPO_DIR" ]; then
    git clone --branch "$TUNNEL_BRANCH" "$TUNNEL_REPO" "$REPO_DIR" ||
        { say "clone failed (network?)"; exit 1; }
    CHANGED=1
fi

# --- ownership: git refuses repos owned by another user ("dubious
# ownership"), which Moonraker reports as "No git repo detected"
if [ -n "$(find "$REPO_DIR" ! -user "$OC_USER" -print -quit 2>/dev/null)" ]; then
    if sudo -n chown -R "$OC_USER:$(id -gn)" "$REPO_DIR" 2>/dev/null; then
        say "gave $REPO_DIR back to $OC_USER"
        CHANGED=1
    else
        say "$REPO_DIR has files not owned by $OC_USER; run:"
        say "  sudo chown -R $OC_USER:$(id -gn) $REPO_DIR"
        exit 1
    fi
fi

# --- no git here (copied tree) or a broken .git: start one in place
top=$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null || echo "")
if [ "$top" != "$REPO_DIR" ]; then
    if [ -e "$REPO_DIR/.git" ]; then
        aside="$REPO_DIR/.git.broken-$(date +%Y%m%d-%H%M%S)"
        mv "$REPO_DIR/.git" "$aside" && say "moved a broken .git to $aside"
    fi
    git init -q "$REPO_DIR" || { say "git init failed in $REPO_DIR"; exit 1; }
    say "started a git repo in $REPO_DIR"
    CHANGED=1
fi

origin=$(g remote get-url origin 2>/dev/null || echo "")
if [ "$(norm_url "$origin")" != "$(norm_url "$TUNNEL_REPO")" ]; then
    if [ -n "$origin" ]; then
        g remote set-url origin "$TUNNEL_REPO"
    else
        g remote add origin "$TUNNEL_REPO"
    fi
    say "origin set to $TUNNEL_REPO (was '${origin:-none}')"
    CHANGED=1
fi

# --- sync onto origin/main, tracking it
if ! g fetch --quiet origin "+refs/heads/$TUNNEL_BRANCH:refs/remotes/origin/$TUNNEL_BRANCH"; then
    say "fetching origin/$TUNNEL_BRANCH failed (network?)"
    g rev-parse --verify -q HEAD >/dev/null || exit 1
else
    branch=$(g symbolic-ref -q --short HEAD || echo "")
    upstream=$(g rev-parse --abbrev-ref -q "$TUNNEL_BRANCH@{upstream}" 2>/dev/null || echo "")
    if ! g rev-parse --verify -q HEAD >/dev/null; then
        # Fresh repo over existing files: adopt origin's history, keep the
        # files, then stash whatever differs from it.
        g symbolic-ref HEAD "refs/heads/$TUNNEL_BRANCH"
        g reset -q "origin/$TUNNEL_BRANCH"
        stash_local
        g branch --quiet -u "origin/$TUNNEL_BRANCH" "$TUNNEL_BRANCH"
        say "on $TUNNEL_BRANCH, tracking origin/$TUNNEL_BRANCH"
        CHANGED=1
    elif [ "$branch" != "$TUNNEL_BRANCH" ] || [ "$upstream" != "origin/$TUNNEL_BRANCH" ]; then
        stash_local
        g checkout --quiet -B "$TUNNEL_BRANCH" "origin/$TUNNEL_BRANCH"
        g branch --quiet -u "origin/$TUNNEL_BRANCH" "$TUNNEL_BRANCH"
        say "switched to $TUNNEL_BRANCH (was '${branch:-detached HEAD}')"
        CHANGED=1
    fi
fi

# --- service, moonraker.asvc, moonraker.conf
out=$(OC_REPO_DIR=$REPO_DIR TUNNEL_REPO=$TUNNEL_REPO TUNNEL_BRANCH=$TUNNEL_BRANCH \
    sh "$REPO_DIR/pi/opencreator-updater/install.sh") ||
    { [ -n "$out" ] && echo "$out"; say "install.sh failed"; exit 1; }
if [ -n "$out" ]; then
    echo "$out"
    echo "$out" | grep -qv 'warning:' && CHANGED=1
fi

if [ "$CHANGED" = 1 ]; then
    if curl -fsS --max-time 10 -X POST "${OC_MOONRAKER_API:-http://127.0.0.1:7125}/server/restart" >/dev/null 2>&1; then
        say "asked Moonraker to restart"
    else
        say "restart Moonraker to pick up the changes (sudo systemctl restart moonraker)"
    fi
else
    say "nothing to fix"
fi
