# Cancel during print start. Every cancel (Fluidd, Mainsail, the screen, the
# API) goes through Moonraker, which then waits for the command Klipper is
# running. Print start is one long command, so the cancel used to wait for all
# of it. Moonraker announces a cancel first (klippy_apis:cancel_requested): this
# sends _OC_START_CANCEL_INTERRUPT then, which runs at once (a script with
# INTERRUPT in it skips Klipper's G-code queue) and makes print start stop at
# its next step. See _OC_START_CHECK in printer.creator5.cfg.
#
# This file is free software; you can redistribute it and/or modify it under
# the terms of the GNU GPLv3 license.
from __future__ import annotations
import logging

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper

INTERRUPT_MACRO = "_OC_START_CANCEL_INTERRUPT"


class OpenCreatorCancel:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.server.register_event_handler(
            "klippy_apis:cancel_requested", self._on_cancel_requested)

    async def _on_cancel_requested(self) -> None:
        klippy_apis = self.server.lookup_component("klippy_apis")
        try:
            # None: no error if the config predates the macro, or Klipper
            # isn't ready; the cancel itself goes ahead either way
            await klippy_apis.run_gcode(INTERRUPT_MACRO, None)
        except Exception:
            logging.exception("opencreator_cancel: %s failed", INTERRUPT_MACRO)


def load_component(config: ConfigHelper) -> OpenCreatorCancel:
    return OpenCreatorCancel(config)
