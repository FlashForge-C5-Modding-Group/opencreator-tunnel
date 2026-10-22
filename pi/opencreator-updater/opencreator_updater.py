#!/usr/bin/env python3
# OpenCreator updater: merges config template updates into the live Klipper
# config, keeping the user's own edits and the printer's SAVE_CONFIG
# calibration. One file for both installs: the printer SoC
# (99-opencreator-updater.sh, "soc") and tunnel SBCs (opencreator-tunnel's
# pi/opencreator-updater, "pi"). opencreator-tunnel keeps an identical copy.
# How it works: config-docs/UPDATER.md in opencreator-fs.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Bumped when a one-time migration must run again on every config (v2:
# re-recover bases from git history, after some installs got wrong ones).
MERGE_VERSION = "2"
SAVE_CONFIG = re.compile(rb"^#\*# <-* SAVE_CONFIG -*>", re.M)
MOONRAKER = "http://127.0.0.1:7125"

# printer SoC
OCDIR = Path("/usr/data/opencreator")
SOC_CONFIG = OCDIR / "klipper" / "config"
SOC_CLONE = OCDIR / "opencreator-fs"
SOC_KLIPPER = OCDIR / "klipper" / "klipper"
FS_GIT_URL = "https://github.com/FlashForge-C5-Modding-Group/opencreator-fs.git"
LOOPSCRIPT_PARTS = ("startup", "firmware", "tunneled", "moonraker", "fluidd", "plugin-manager")
NGINX_CONF_D = Path("/usr/data/nginx/conf.d")
FIRMWARE_EXE = Path("/usr/prog/PROGRAM/software/firmwareExe")
STOCK_STARTUP = (Path("/usr/prog/app_startup.sh"), Path("/usr/prog/PROGRAM/runFirmwareExe.sh"))
SOC_LOG = Path("/tmp/opencreator-updater.log")

MODEL_FILE = "printer.model.cfg"
PRO_MODEL = "%s: Creator 5 Pro\n[include printer.pro.cfg]\n"
C5_MODEL = "%s: Creator 5 (no chamber heater, chamber fans or aux fan)\n"


class UpdateError(Exception):
    pass


def normalize(data: bytes) -> bytes:
    """What is compared and merged: no CR, and no SAVE_CONFIG block (the
    printer's own calibration, which is carried through the merge as is)."""
    data = data.replace(b"\r", b"")
    m = SAVE_CONFIG.search(data)
    return data[:m.start()] if m else data


def write_atomic(path: Path, data: bytes, mode: int | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        raise UpdateError("%s is a directory" % path)
    if mode is None:    # keep the file's permissions (mkstemp would make it 0600)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".oc-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


class Updater:
    def __init__(self, config_dir, repo, template_rel, patterns=("*.cfg",), log_path=None,
                 moonraker=MOONRAKER, model_detect=None, out=sys.stdout):
        self.config = Path(config_dir)
        self.state = self.config / ".opencreator-updater"
        self.base_dir = self.state / "base"
        self.applied = self.state / "applied-commit"
        self.merge_version = self.state / "merge-version"
        self.repo = Path(repo)
        self.template_rel = template_rel
        self.patterns = patterns
        self.log_path = Path(log_path) if log_path else None
        self.moonraker = moonraker
        self.model_detect = model_detect
        self.out = out
        self._history = None      # (repo path, complete?) once looked up this run
        self._work = None

    # ---- plumbing -------------------------------------------------------------
    def log(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(line + "\n")
        else:
            print(line, file=self.out, flush=True)

    def git(self, *args, cwd=None, check=True) -> bytes:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        env["PATH"] = env.get("PATH", "") + ":/opt/bin"
        r = subprocess.run(["git"] + list(args), cwd=str(cwd or self.repo), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and r.returncode != 0:
            raise UpdateError("git %s: %s" % (args[0], r.stderr.decode("utf-8", "replace").strip()))
        return r.stdout

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").decode().strip()

    def api(self, path, method="GET", body=None, timeout=10):
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(self.moonraker + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if body is not None else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def printing(self) -> bool:
        try:
            st = self.api("/printer/objects/query?print_stats=state", timeout=5)
            return st["result"]["status"]["print_stats"]["state"] in ("printing", "paused")
        except Exception:
            return False

    # ---- config merge ---------------------------------------------------------
    def templates(self, template_dir: Path):
        found = set()
        for pattern in self.patterns:
            found.update(p for p in template_dir.glob(pattern) if p.is_file())
        return sorted(found)

    def publish(self, name, live: Path, live_data: bytes, base: Path, base_data: bytes):
        """The live config and its base, together: if the base can't be written,
        the live file goes back to what it was."""
        old = live.read_bytes() if live.exists() else None
        write_atomic(live, live_data)
        try:
            write_atomic(base, base_data)
        except Exception:
            if old is None:
                live.unlink(missing_ok=True)
            else:
                write_atomic(live, old)
            raise

    def _search(self, repo: Path, rel: str, want: bytes) -> bytes | None:
        commits = self.git("log", "--diff-filter=AM", "--format=%H", "HEAD", "--", rel, cwd=repo).split()
        for commit in commits:
            raw = self.git("show", "%s:%s" % (commit.decode(), rel), cwd=repo)
            if normalize(raw) == want:
                return raw
        return None

    def recover_base(self, name: str, live_n: bytes):
        """The template version the live file is an unedited copy of, from git
        history: (0, raw) found, (1, None) no exact match, (2, None) no history."""
        rel = "%s/%s" % (self.template_rel, name)
        try:
            if self._history is None:
                shallow = self.git("rev-parse", "--is-shallow-repository").strip() == b"true"
                self._history = (self.repo, not shallow)
            repo, complete = self._history
            found = self._search(repo, rel, live_n)
            if found is not None:
                return 0, found
            if complete:
                return 1, None
            # A shallow checkout's miss isn't conclusive: look in the full history.
            origin = self.git("remote", "get-url", "origin").decode().strip()
            full = Path(self._workdir()) / "history"
            self.git("clone", "-q", "--no-checkout", origin, str(full), cwd=self._workdir())
            self._history = (full, True)
            found = self._search(full, rel, live_n)
            return (0, found) if found is not None else (1, None)
        except (UpdateError, OSError) as e:
            self.log("%s: history lookup failed: %s" % (name, e))
            return 2, None

    def _workdir(self):
        if self._work is None:
            self._work = tempfile.mkdtemp(prefix="opencreator-updater.")
        return self._work

    def merge3(self, live: bytes, base: bytes, latest: bytes):
        """git merge-file: (status, merged). status 0 clean, >0 conflicts, <0 error."""
        d = Path(self._workdir())
        paths = [d / "current", d / "base", d / "latest"]
        for p, data in zip(paths, (live, base, latest)):
            p.write_bytes(data)
        env = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/opt/bin")
        r = subprocess.run(["git", "merge-file", "-p", "-L", "current", "-L", "base", "-L", "latest"]
                           + [str(p) for p in paths], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if r.returncode < 0 or r.returncode > 127:
            return -1, r.stderr
        return r.returncode, r.stdout

    def merge_configs(self, template_dir: Path):
        """Returns (changed, conflicts)."""
        changed, conflicts = False, []
        migration = self._read(self.merge_version) != MERGE_VERSION
        templates = self.templates(template_dir)
        if not templates:
            self.log("CONFLICT -- config templates missing in %s" % template_dir)
            return False, ["config-templates"]
        for template in templates:
            name = template.relative_to(template_dir).as_posix()
            live, base = self.config / name, self.base_dir / name
            tmpl = template.read_bytes()
            try:
                if not live.exists():
                    self.publish(name, live, tmpl, base, tmpl)
                    self.log("%s: new" % name)
                    changed = True
                    continue
                if not migration and base.is_file() and base.read_bytes() == tmpl:
                    continue                                    # unchanged
                live_data = live.read_bytes()
                tmpl_n, live_n = normalize(tmpl), normalize(live_data)
                if live_n == tmpl_n:
                    # Already current: only the base needs to catch up.
                    if not base.is_file() or base.read_bytes() != tmpl:
                        write_atomic(base, tmpl)
                        self.log("%s: current config, baseline synchronized" % name)
                    continue
                merge_base = base.read_bytes() if base.is_file() else None
                if merge_base is None or migration:
                    status, recovered = self.recover_base(name, live_n)
                    if status == 0:
                        merge_base = recovered
                        self.log("%s: recovered historical baseline" % name)
                    elif status == 1 and merge_base is not None:
                        if merge_base == tmpl:
                            self.log("%s: unchanged template, preserving user edits" % name)
                            continue
                        # No exact match: keep the known base and merge onto the edits.
                    else:
                        conflicts.append(name)
                        self.log("%s: CONFLICT -- no safe historical baseline (lookup status %d); "
                                 "leaving live and base untouched" % (name, status))
                        continue
                rc, merged = self.merge3(live_data.replace(b"\r", b""), normalize(merge_base), tmpl_n)
                conflict_file = live.with_name(live.name + ".conflict")
                if rc == 0:
                    self.publish(name, live, merged, base, tmpl)
                    conflict_file.unlink(missing_ok=True)
                    self.log("%s: merged" % name)
                    changed = True
                elif rc > 0:
                    # Never conflict markers in a file Klipper reads: the attempt goes beside it.
                    write_atomic(conflict_file, merged)
                    conflicts.append(name)
                    self.log("%s: CONFLICT -- your edits and the update change the same lines; "
                             "see %s" % (name, conflict_file.name))
                else:
                    conflicts.append(name)
                    self.log("%s: CONFLICT -- merge failed: %s" % (name, merged.decode("utf-8", "replace")))
            except (OSError, UpdateError) as e:
                conflicts.append(name)
                self.log("%s: CONFLICT -- %s; live and base left as they were" % (name, e))
        if self.ensure_model_file():
            changed = True
        return changed, conflicts

    def ensure_model_file(self) -> bool:
        """printer.base.cfg only gets the Pro hardware through printer.model.cfg.
        Installs from before it existed had that hardware, so they keep it."""
        path = self.config / MODEL_FILE
        if path.exists() or not (self.config / "printer.pro.cfg").exists():
            return False
        machine = self.model_detect() if self.model_detect else None
        text = (C5_MODEL if machine == "Creator5" else PRO_MODEL) % "# Written by the updater"
        write_atomic(path, text.encode())
        self.log("%s: written (%s)" % (MODEL_FILE, "Creator 5" if machine == "Creator5" else "Creator 5 Pro"))
        return True

    def record_success(self, conflicts, head=None):
        """Mark the merge done, but only when every config merged cleanly."""
        if conflicts:
            return
        write_atomic(self.merge_version, (MERGE_VERSION + "\n").encode())
        if head:
            write_atomic(self.applied, (head + "\n").encode())

    def restart_klipper(self, changed, conflicts, service=False):
        if conflicts:
            self.log("CONFLICTS in: %s -- resolve by hand, then restart Klipper" % " ".join(conflicts))
            return
        if not changed:
            self.log("nothing changed")
            return
        while self.printing():
            time.sleep(60)
        path = "/machine/services/restart?service=klipper" if service else "/printer/restart"
        try:
            self.api(path, "POST")
            self.log("klipper %s requested via Moonraker" % ("service restart" if service else "restart"))
        except Exception as e:
            self.log("klipper restart request failed (%s); restart it yourself" % e)

    def cleanup(self):
        if self._work:
            shutil.rmtree(self._work, ignore_errors=True)
            self._work = None
        self._history = None

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text().strip()
        except OSError:
            return ""


# ---- printer SoC ----------------------------------------------------------------

def overlay_dir(src: Path, dest: Path, manifest: Path):
    """Copy SRC's files over DEST (files only DEST has stay), and write the new
    manifest beside the old one; a file the old manifest lists that SRC no
    longer has is removed from DEST if unchanged. The installer's oc_overlay
    writes the same "md5  path" manifest."""
    lines = []
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src).as_posix()
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(f), str(dest / rel))
        lines.append("%s  %s" % (md5(f), rel))
    if manifest.is_file():
        for line in manifest.read_text().splitlines():
            if "  " not in line:
                continue
            rel = line.split("  ", 1)[1]
            if (src / rel).is_file() or not (dest / rel).is_file():
                continue
            if "%s  %s" % (md5(dest / rel), rel) == line:
                (dest / rel).unlink()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    Path(str(manifest) + ".new").write_text("".join(l + "\n" for l in lines))


def part_current(src: Path, dest: Path) -> bool:
    for f in src.rglob("*"):
        if f.is_file():
            d = dest / f.relative_to(src)
            if not d.is_file() or d.read_bytes() != f.read_bytes():
                return False
    return True


def detect_machine():
    for path in STOCK_STARTUP:
        try:
            m = re.search(r"^\s*MACHINE=([A-Za-z0-9]+)", path.read_text(errors="replace"), re.M)
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


class SocUpdater(Updater):
    """The printer: watch the opencreator-fs checkout (Moonraker's
    [update_manager opencreator] updates it), apply each new commit."""

    def __init__(self, config_dir=SOC_CONFIG, clone=SOC_CLONE, ocdir=OCDIR, log_path=SOC_LOG, **kw):
        kw.setdefault("model_detect", detect_machine)
        super().__init__(config_dir, clone, "fs/usr/data/config", log_path=log_path, **kw)
        self.ocdir = Path(ocdir)
        self.loopscript_applied = self.ocdir / ".loopscript-commit"
        self.nginx = NGINX_CONF_D
        self.firmware_exe = FIRMWARE_EXE

    def ensure_clone(self) -> bool:
        if (self.repo / ".git").is_dir():
            return True
        # Installs from before the checkout was kept: make it now (it's what
        # Moonraker's update manager pulls, too).
        try:
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            self.git("clone", "-q", "--depth", "1", FS_GIT_URL, str(self.repo), cwd=self.repo.parent)
            self.log("cloned opencreator-fs into %s" % self.repo)
            return True
        except UpdateError as e:
            self.log("no opencreator-fs checkout and cloning failed (%s); retrying later" % e)
            shutil.rmtree(self.repo, ignore_errors=True)
            return False

    def sync_loopscript(self):
        """OpenCreator's own folders: each is copied next to the live one, the new
        files laid over the copy (user-added files stay) and it is swapped in, so
        running scripts keep their old copy; the new ones run from the next boot."""
        src_root = self.repo / "fs" / "usr" / "data"
        changed = False
        (self.ocdir / ".installed").mkdir(parents=True, exist_ok=True)
        for part in LOOPSCRIPT_PARTS:
            src, dest = src_root / "opencreator" / part, self.ocdir / part
            if not src.is_dir() or part_current(src, dest):
                continue
            new, old = Path(str(dest) + ".new"), Path(str(dest) + ".old")
            manifest = self.ocdir / ".installed" / (part + ".md5")
            shutil.rmtree(new, ignore_errors=True)
            shutil.rmtree(old, ignore_errors=True)
            try:
                if dest.is_dir():
                    shutil.copytree(str(dest), str(new), symlinks=True)
                overlay_dir(src, new, manifest)
            except OSError as e:
                self.log("%s: copy failed: %s" % (part, e))
                shutil.rmtree(new, ignore_errors=True)
                continue
            if part == "startup":
                self._chmod(new / "startup.sh", 0o755)
                for s in (new / "scripts").glob("*.sh"):
                    self._chmod(s, 0o755)
            elif part == "firmware":
                self._chmod(new / "firmwareExe", 0o755)
            elif part == "plugin-manager":
                self._chmod(new / "oc-plugin", 0o755)
            elif part == "tunneled":
                # executable means tunnel mode: keep whichever mode is set
                tunnel = (dest / "tunneled.sh").is_file() and os.access(str(dest / "tunneled.sh"), os.X_OK)
                self._chmod(new / "tunneled.sh", 0o755 if tunnel else 0o644)
            if dest.is_dir():
                os.rename(str(dest), str(old))
            os.rename(str(new), str(dest))
            os.replace(str(manifest) + ".new", str(manifest))
            shutil.rmtree(old, ignore_errors=True)
            self.log("loopscript %s: updated" % part)
            changed = True
        version = src_root / "opencreator" / "version.txt"
        if version.is_file() and (not (self.ocdir / "version.txt").is_file()
                                  or (self.ocdir / "version.txt").read_bytes() != version.read_bytes()):
            shutil.copy2(str(version), str(self.ocdir / "version.txt"))
        for conf in sorted((src_root / "nginx" / "conf.d").glob("*.conf")):
            dest = self.nginx / conf.name
            if dest.is_file() and dest.read_bytes() == conf.read_bytes():
                continue
            self.nginx.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(conf), str(dest))
            self.log("nginx %s: updated" % conf.name)
            changed = True
        # Only while the LoopScript launcher (a shell script, not FlashForge's
        # ELF) is in place, so a rollback to stock is never undone.
        launcher = self.ocdir / "firmware" / "firmwareExe"
        try:
            current = self.firmware_exe.read_bytes() if self.firmware_exe.is_file() else b""
            if launcher.is_file() and current[1:4] != b"ELF" and current != launcher.read_bytes():
                write_atomic(self.firmware_exe, launcher.read_bytes(), 0o755)
                self.log("firmwareExe: updated")
                changed = True
        except OSError as e:
            self.log("firmwareExe: update failed: %s" % e)
        os.sync()
        if changed:
            try:
                self.api("/printer/gcode/script", "POST", {"script": 'RESPOND MSG="OpenCreator startup files '
                         'updated; they take effect after the next reboot."'})
            except Exception:
                pass
        return changed

    @staticmethod
    def _chmod(path: Path, mode):
        if path.is_file():
            os.chmod(str(path), mode)

    def apply(self, restart=True, loopscript=True):
        if not self.ensure_clone():
            return
        try:
            head = self.head()
        except UpdateError as e:
            self.log("can't read the opencreator-fs checkout: %s" % e)
            return
        if loopscript:
            recorded = self._read(self.loopscript_applied)
            if not recorded:
                # The installed files came from this checkout (or are newer):
                # only note where it is, sync from the next update.
                self.log("LoopScript files: tracking from opencreator-fs %s" % head)
                write_atomic(self.loopscript_applied, (head + "\n").encode())
            elif recorded != head:
                self.log("syncing LoopScript files from opencreator-fs %s" % head)
                self.sync_loopscript()
                write_atomic(self.loopscript_applied, (head + "\n").encode())
        if self._read(self.applied) == head and self._read(self.merge_version) == MERGE_VERSION:
            return
        self.log("applying opencreator-fs %s" % head)
        try:
            changed, conflicts = self.merge_configs(self.repo / self.template_rel)
            self.record_success(conflicts, head)
        finally:
            self.cleanup()
        if restart:
            self.restart_klipper(changed, conflicts)

    def watch(self):
        self.log("watching %s (checks every 60 s)" % self.repo)
        for _ in range(120):                    # Moonraker up, so a restart can reach it
            try:
                self.api("/server/info", timeout=5)
                break
            except Exception:
                time.sleep(5)
        while True:
            try:
                self.apply()
            except Exception as e:              # keep watching whatever happens
                self.log("update failed: %s" % e)
            time.sleep(60)


# ---- tunnel SBC -------------------------------------------------------------------

class PiUpdater(Updater):
    """A tunnel SBC: run once after Moonraker's update manager pulls
    opencreator-tunnel, then pull Klipper."""

    def __init__(self, config_dir=None, repo=None, **kw):
        home = Path.home()
        repo = Path(repo) if repo else Path(__file__).resolve().parents[2]
        config = config_dir or os.environ.get("OC_UPDATER_CONFIG_DIR", str(home / "printer_data" / "config"))
        self.klipper = Path(os.environ.get("OC_UPDATER_KLIPPER_DIR", str(home / "klipper")))
        kw.setdefault("moonraker", os.environ.get("OC_UPDATER_MOONRAKER_API", MOONRAKER))
        super().__init__(config, repo, "pi/config", patterns=("*.cfg", ".fluidd-theme/*"), **kw)

    def pull_klipper(self) -> bool:
        if not (self.klipper / ".git").is_dir():
            self.log("klipper: %s is not a git checkout, skipping" % self.klipper)
            return False
        try:
            before = self.git("rev-parse", "HEAD", cwd=self.klipper).strip()
            self.git("fetch", "--quiet", cwd=self.klipper)
            self.git("merge", "--ff-only", "--quiet", "@{u}", cwd=self.klipper)
            after = self.git("rev-parse", "HEAD", cwd=self.klipper).strip()
        except UpdateError as e:
            self.log("klipper: left as it is (%s)" % e)
            return False
        self.log("klipper: %s" % ("updated %s -> %s" % (before[:9].decode(), after[:9].decode())
                                  if before != after else "already up to date"))
        return before != after

    def run(self, restart=True):
        self.log("OpenCreator Updater: syncing %s from %s" % (self.config, self.repo / self.template_rel))
        try:
            head = self.head()
        except UpdateError:
            head = None
        try:
            changed, conflicts = self.merge_configs(self.repo / self.template_rel)
            self.record_success(conflicts, head)
        finally:
            self.cleanup()
        code = self.pull_klipper()
        if restart:
            if conflicts and code:
                # the conflicted files are untouched and still valid: load the new code
                self.restart_klipper(True, [], service=True)
            self.restart_klipper(changed or code, conflicts, service=code)
        return 1 if conflicts else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenCreator config updater")
    ap.add_argument("where", choices=("soc", "pi"))
    ap.add_argument("--once", action="store_true", help="soc: apply once instead of watching")
    ap.add_argument("--config-dir", help="merge into this config folder instead (testing)")
    ap.add_argument("--repo", help="take the templates from this checkout instead (testing)")
    ap.add_argument("--no-restart", action="store_true", help="don't restart Klipper")
    ap.add_argument("--no-loopscript", action="store_true", help="soc: leave OpenCreator's own folders alone")
    ap.add_argument("--stdout", action="store_true", help="soc: log to the terminal")
    a = ap.parse_args(argv)
    if a.where == "pi":
        return PiUpdater(config_dir=a.config_dir, repo=a.repo).run(restart=not a.no_restart)
    kw = {"log_path": None} if a.stdout else {}
    u = SocUpdater(config_dir=Path(a.config_dir) if a.config_dir else SOC_CONFIG,
                   clone=Path(a.repo) if a.repo else SOC_CLONE, **kw)
    if a.once or a.config_dir:
        u.apply(restart=not a.no_restart, loopscript=not a.no_loopscript and not a.config_dir)
        return 0
    u.watch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
