# Accept sliced 3MF uploads (OrcaSlicer / Bambu Studio "gcode.3mf") in the
# gcodes root by repacking them as UFP, which Moonraker already unpacks.
#
# This file is free software; you can redistribute it and/or modify it under
# the terms of the GNU GPLv3 license.
from __future__ import annotations
import os
import re
import shutil
import tempfile
import zipfile
import logging

from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .file_manager.file_manager import FileManager

PLATE_RE = re.compile(r"^/?Metadata/plate_(\d+)\.gcode$")
UFP_MODEL_PATH = "/3D/model.gcode"
UFP_THUMB_PATH = "/Metadata/thumbnail.png"
GCODE_EXTS = (".gcode", ".g", ".gco")
GCODE_SETTING_RE = re.compile(rb"^; \w+_gcode = [^\n]*", re.M)
ESTIMATE_TEXT = b"estimated printing time"
ESTIMATE_HIDDEN = b"estimated_printing_time"
FOOTER_SIZE = 1024 * 1024
CHUNK_SIZE = 1024 * 1024


def hide_estimate_templates(path: str) -> bool:
    start = max(0, os.path.getsize(path) - FOOTER_SIZE)
    changed = False
    with open(path, "rb+") as f:
        f.seek(start)
        footer = f.read()
        for match in GCODE_SETTING_RE.finditer(footer):
            line = match.group(0)
            index = line.find(ESTIMATE_TEXT)
            while index >= 0:
                f.seek(start + match.start() + index)
                f.write(ESTIMATE_HIDDEN)
                changed = True
                index = line.find(ESTIMATE_TEXT, index + 1)
    return changed


def find_plate(names: List[str]) -> Tuple[str, Optional[str]]:
    plates: List[Tuple[int, str]] = []
    for name in names:
        match = PLATE_RE.match(name)
        if match is not None:
            plates.append((int(match.group(1)), name))
    if not plates:
        raise ValueError("3MF contains no sliced plate gcode")
    gcode = min(plates)[1]
    thumb = gcode[:-len(".gcode")] + ".png"
    return gcode, (thumb if thumb in names else None)


def repack_as_ufp(path: str) -> None:
    with zipfile.ZipFile(path) as src:
        gcode, thumb = find_plate(src.namelist())
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
        os.close(fd)
        fd, gcode_path = tempfile.mkstemp(dir=os.path.dirname(path))
        os.close(fd)
        try:
            with src.open(gcode) as fin, open(gcode_path, "wb") as fout:
                shutil.copyfileobj(fin, fout, CHUNK_SIZE)
            hide_estimate_templates(gcode_path)
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as dst:
                with open(gcode_path, "rb") as fin, dst.open(UFP_MODEL_PATH, "w") as fout:
                    shutil.copyfileobj(fin, fout, CHUNK_SIZE)
                if thumb is not None:
                    dst.writestr(UFP_THUMB_PATH, src.read(thumb))
        except Exception:
            os.remove(tmp_path)
            raise
        finally:
            os.remove(gcode_path)
    os.replace(tmp_path, path)


def ufp_name(filename: str) -> str:
    # "part.gcode.3mf" and "part.3mf" both become "part.ufp"
    base = filename[:-len(".3mf")]
    if base.lower().endswith(".gcode"):
        base = base[:-len(".gcode")]
    return base + ".ufp"


class OpenCreator3mf:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.event_loop = self.server.get_event_loop()
        fm: FileManager = self.server.lookup_component("file_manager")
        self.finalize_upload = fm.finalize_upload
        setattr(fm, "finalize_upload", self._finalize_upload)

    async def _finalize_upload(self, form_args: Dict[str, Any]) -> Dict[str, Any]:
        filename: str = form_args.get("filename", "")
        root: str = form_args.get("root", "gcodes").lower()
        if root == "gcodes" and filename.lower().endswith(".3mf"):
            tmp_path: str = form_args["tmp_file_path"]
            try:
                await self.event_loop.run_in_thread(repack_as_ufp, tmp_path)
            except Exception as e:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                raise self.server.error(f"Unable to read 3MF {filename}: {e}")
            logging.info(f"3MF upload {filename} repacked for gcode extraction")
            form_args = dict(form_args, filename=ufp_name(filename))
        elif root == "gcodes" and filename.lower().endswith(GCODE_EXTS):
            try:
                changed = await self.event_loop.run_in_thread(
                    hide_estimate_templates, form_args["tmp_file_path"])
            except Exception:
                logging.exception(f"Unable to check time estimate in {filename}")
            else:
                if changed:
                    logging.info(f"{filename}: hid slicer estimate template")
        return await self.finalize_upload(form_args)


def load_component(config: ConfigHelper) -> OpenCreator3mf:
    return OpenCreator3mf(config)
