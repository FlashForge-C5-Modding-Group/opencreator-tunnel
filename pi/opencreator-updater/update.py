#!/usr/bin/env python3
"""OpenCreator Updater: bring printer_data/config up to date with the
tunneled template in this repo (pi/config/*.cfg), then pull Klipper,
without clobbering values the user has customized (speeds, spoolman,
toggle defaults, etc).

How it stays non-destructive: every managed config file has a tracked
"base" snapshot -- the template content from the last time this script
ran successfully. Each run does a real three-way merge (git merge-file)
of (live file, base snapshot, new template) for every file: lines the
template changed get applied, lines the user changed independently are
left alone, and only a genuine overlapping edit raises a conflict. The
live file is never touched when that happens -- the attempted merge
(with conflict markers) is written to a sibling "<file>.conflict" file
instead, for a human to resolve and copy over by hand. Conflict markers
must never land in a config Klipper is actively reading.

First run has no base snapshot yet, so there is nothing to diff the
template against: it seeds the base from the current template and
leaves the live files untouched. Real auto-merging starts from the
*second* run onward. This is intentional -- there is no reliable way to
know what template version a pre-existing live file actually started
from, so the honest thing to do is start tracking from here rather than
guess.

Invoked as a one-shot systemd service that Moonraker's update_manager
restarts after it pulls this repo (see opencreator-updater.service and
the [update_manager OpenCreator Updater] section in moonraker.conf).
"""
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import urllib.error

REPO_DIR = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_DIR / "pi" / "config"
CONFIG_DIR = pathlib.Path(
    os.environ.get("OC_UPDATER_CONFIG_DIR",
                   pathlib.Path.home() / "printer_data" / "config"))
BASE_DIR = CONFIG_DIR / ".opencreator-updater" / "base"
KLIPPER_DIR = pathlib.Path(
    os.environ.get("OC_UPDATER_KLIPPER_DIR", pathlib.Path.home() / "klipper"))
MOONRAKER_API = os.environ.get("OC_UPDATER_MOONRAKER_API",
                               "http://127.0.0.1:7125")


def log(msg):
    print(msg, flush=True)


def _normalize_newlines(src, dest):
    dest.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))


def merge_one(name, live_path, base_path, template_path):
    """Three-way merge template_path's changes into live_path, using
    base_path as the common ancestor. Returns 'unchanged', 'merged',
    'conflict', or 'new'."""
    if not live_path.exists():
        shutil.copy2(template_path, live_path)
        return "new"
    if not base_path.exists():
        # First time tracking this file: nothing to diff against yet.
        return "bootstrap"
    if base_path.read_bytes() == template_path.read_bytes():
        return "unchanged"
    # Normalize line endings before diffing. A live file that picked up
    # CRLF endings at some point (editing on Windows, an earlier scp,
    # whatever) would otherwise differ from the LF-only base/template
    # on every single line -- git merge-file then finds no common
    # anchor points at all and conflicts the *entire* file (this
    # actually happened once in production: a trivial one-line
    # template change turned into an 800-line whole-file conflict that
    # got written over a live config and halted Klipper). Klipper's own
    # config parser doesn't care about line endings, so merging and
    # writing the result back as LF-only is safe and keeps every future
    # base snapshot consistently LF too.
    work = live_path.with_suffix(live_path.suffix + ".oc-merge-tmp")
    norm_base = base_path.with_suffix(base_path.suffix + ".oc-merge-base")
    norm_latest = template_path.with_suffix(
        template_path.suffix + ".oc-merge-latest")
    try:
        _normalize_newlines(live_path, work)
        _normalize_newlines(base_path, norm_base)
        _normalize_newlines(template_path, norm_latest)
        result = subprocess.run(
            ["git", "merge-file", "-L", "current", "-L", "base", "-L", "latest",
             str(work), str(norm_base), str(norm_latest)],
            capture_output=True)
        if result.returncode == 0:
            shutil.move(str(work), str(live_path))
            return "merged"
        if result.returncode > 0:
            # A real conflict: the template changed a line the live
            # file also changed. Never write conflict markers into the
            # live file itself -- that would corrupt a config Klipper
            # is actively reading (which is exactly what happened
            # before this safeguard existed). Leave live_path
            # completely untouched and write the attempted merge
            # (conflict markers and all) to a sibling .conflict file
            # for a human to resolve and copy over by hand.
            conflict_path = live_path.with_suffix(live_path.suffix + ".conflict")
            shutil.move(str(work), str(conflict_path))
            return "conflict"
        log("%s: git merge-file failed outright: %s"
            % (name, result.stderr.decode("utf-8", "replace")))
        return "error"
    finally:
        for tmp in (work, norm_base, norm_latest):
            tmp.unlink(missing_ok=True)


def sync_configs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    for template_path in sorted(TEMPLATE_DIR.glob("*.cfg")):
        name = template_path.name
        live_path = CONFIG_DIR / name
        base_path = BASE_DIR / name
        outcome = merge_one(name, live_path, base_path, template_path)
        results[name] = outcome
        if outcome in ("merged", "new", "bootstrap", "unchanged"):
            shutil.copy2(template_path, base_path)
        log("%-28s %s" % (name, outcome))
    return results


def pull_klipper():
    if not (KLIPPER_DIR / ".git").is_dir():
        log("klipper: %s is not a git checkout, skipping" % (KLIPPER_DIR,))
        return False
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=KLIPPER_DIR,
                            capture_output=True, text=True).stdout.strip()
    fetch = subprocess.run(["git", "fetch", "--quiet"], cwd=KLIPPER_DIR,
                           capture_output=True, text=True)
    if fetch.returncode != 0:
        log("klipper: fetch failed: %s" % (fetch.stderr.strip(),))
        return False
    pull = subprocess.run(["git", "merge", "--ff-only", "--quiet", "@{u}"],
                          cwd=KLIPPER_DIR, capture_output=True, text=True)
    if pull.returncode != 0:
        log("klipper: not fast-forwardable, leaving as-is "
            "(local commits or diverged): %s" % (pull.stderr.strip(),))
        return False
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=KLIPPER_DIR,
                           capture_output=True, text=True).stdout.strip()
    changed = before != after
    log("klipper: %s" % (("updated %s -> %s" % (before[:9], after[:9]))
                         if changed else "already up to date"))
    return changed


def restart_klipper():
    req = urllib.request.Request(
        MOONRAKER_API + "/printer/restart", method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        log("klipper: restart requested via Moonraker API")
    except urllib.error.URLError as exc:
        log("klipper: restart request failed (%s); restart manually" % (exc,))


def main():
    log("OpenCreator Updater: syncing %s from %s"
        % (CONFIG_DIR, TEMPLATE_DIR))
    results = sync_configs()
    conflicts = [n for n, r in results.items() if r == "conflict"]
    config_changed = any(r in ("merged", "new") for r in results.values())
    klipper_changed = pull_klipper()
    if conflicts:
        log("")
        log("CONFLICTS in: %s" % (", ".join(conflicts),))
        log("The live files were left untouched. Review the <<<<<<< current / "
            "======= / >>>>>>> latest markers in each <file>.conflict next to "
            "it, then copy over by hand whatever you want to keep.")
        return 1
    if config_changed or klipper_changed:
        restart_klipper()
    else:
        log("nothing changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
