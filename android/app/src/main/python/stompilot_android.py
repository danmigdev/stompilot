"""Android entry point: the desktop service with Android file pickers and USB MIDI.

Besides the desktop requests, the live view uses two read-only ones:
``tank_state`` (the TANK's preset, volume and effects) and ``chocolate_page``
(the controller's current advanced page).
"""
import json
import threading

from midi_configurator import chocolate_transport
from midi_configurator.chocolate import CURRENT_PAGE_OFFSET, PAGE_COUNT_OFFSET
from midi_configurator.chocolate_transport import UsbChocolate
from midi_configurator.desktop_service import DesktopService
from midi_configurator.tank_check import (STATE, STATE_SIZE, TankError, UsbTank, _model_label, _volume,
                                          enable_offset, model_offset, preset_name)
from midi_configurator.core import TANK_LIVE_PATCH, TANK_LIVE_PATCH_SIZE

import android_midi

TANK_PROFILE = "tank_mini_v09"


class AndroidService:
    def __init__(self, data_directory, documents, hub):
        self.backend = android_midi.Backend(hub)
        chocolate_transport.use_backend(self.backend)
        self._documents = documents
        self._tank_lock = threading.Lock()
        self.service = DesktopService(data_directory=data_directory, dialog=self._pick)

    def _pick(self, kind, suggested_name="", file_types=()):
        path = self._documents.pick(kind, suggested_name or "")
        return None if path is None else str(path)

    def request_json(self, method, payload_json):
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except ValueError:
            return json.dumps({"ok": False, "error": {"message": "The request payload is not valid JSON."}})
        if method == "tank_state":
            result = self._read_only(self.tank_state)
        elif method == "chocolate_page":
            result = self._read_only(self.chocolate_page)
        else:
            result = self.service.request(method, payload)
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _read_only(operation):
        try:
            return {"ok": True, "data": operation()}
        except Exception as error:  # Reported to the live view, which keeps running.
            return {"ok": False, "error": {"message": str(error)}}

    def tank_state(self):
        """The TANK's preset, volume and each block's ON/OFF and model."""
        if not self._tank_lock.acquire(blocking=False):
            raise TankError("The TANK is being read.")
        try:
            profile = self.service.profiles[TANK_PROFILE]
            tank = UsbTank.connect(self.backend, timeout=0.5)
            try:
                name = tank.identify()
                state = tank.read(STATE, STATE_SIZE)
                live = tank.read(TANK_LIVE_PATCH, TANK_LIVE_PATCH_SIZE)
            finally:
                tank.close()
            blocks = [{"id": block["id"], "label": block["label"], "on": live[enable_offset(block)] == 1,
                       "model": _model_label(block, live[model_offset(block)])} for block in profile["blocks"]]
            return {"name": name, "preset": preset_name(state[0]), "volume": _volume(live), "blocks": blocks}
        finally:
            self._tank_lock.release()

    def chocolate_page(self):
        """The controller's current advanced page (1-based) and page count."""
        service = self.service
        if service.transport is not None:
            raise chocolate_transport.ChocolateTransportError(
                "The editor is connected to the Chocolate Plus. Disconnect it to start the live view.")
        devices = list(service.discovery())
        if len(devices) != 1:
            raise chocolate_transport.ChocolateTransportError(
                f"Connect one Chocolate Plus (switch on U); found {len(devices)}.")
        usb = UsbChocolate(devices[0].input_name, devices[0].output_name, timeout=1.0).open()
        try:
            page = usb._read(CURRENT_PAGE_OFFSET, 1)[0]
            count = usb._read(PAGE_COUNT_OFFSET, 1)[0]
        finally:
            usb.close()
        return {"page": page + 1, "pages": count + 1, "device": devices[0].name}


def create(data_directory, documents, hub):
    return AndroidService(str(data_directory), documents, hub)
