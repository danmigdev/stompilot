"""Project defaults, independent of the user interface."""
from copy import deepcopy

try:
    from .core import MAX_SYSEX_BYTES, sysex_limit
except ImportError:
    from core import MAX_SYSEX_BYTES, sysex_limit


def empty_switch(letter):
    return {"label": f"Switch {letter}", "mode": "single",
            "states": [{"label": "Sequence", "actions": []}]}


def empty_page(number):
    return {"name": f"Page {number}",
            "switches": {letter: empty_switch(letter) for letter in "ABCD"}}


def normalize_project(project):
    """Expand optional schema fields before displaying an imported project."""
    result = deepcopy(project)
    controller = result.setdefault("controller", {"id": "chocolate_plus"})
    controller["max_sysex_bytes"] = sysex_limit(result)
    for page in result["pages"]:
        for letter in "ABCD":
            switch = page["switches"].setdefault(letter, empty_switch(letter))
            switch.setdefault("label", f"Switch {letter}")
            for index, state in enumerate(switch["states"], 1):
                state.setdefault("label", f"State {index}")
                state.setdefault("actions", [])
    return result


def new_project(example=True):
    project = {"schema_version": 1, "name": "Tremolo and Boost" if example else "New project",
               "controller": {"id": "chocolate_plus", "max_sysex_bytes": MAX_SYSEX_BYTES},
               "pages": [empty_page(1)]}
    if not example:
        return project
    base = {"device_id": "tank_mini_v09", "channel": 1}
    mod_on = dict(base, block_id="mod", model_id="2", type="enable", value=True)
    boost_on = dict(base, block_id="fx", model_id="1", type="enable", value=True)
    mix10 = dict(base, block_id="rev", model_id="1", type="parameter", param_id="mix", value=10)
    mod_off = dict(mod_on, value=False)
    boost_off = dict(boost_on, value=False)
    mix5 = dict(mix10, value=5)
    switches = project["pages"][0]["switches"]
    switches["A"] = {"label": "Tremolo", "mode": "alternating", "states": [
        {"label": "Tremolo ON", "actions": [mod_on]},
        {"label": "Tremolo OFF", "actions": [mod_off]}]}
    switches["B"] = {"label": "Boost + Reverb", "mode": "alternating", "states": [
        {"label": "Boost ON / Mix 10", "actions": [boost_on, mix10]},
        {"label": "Boost OFF / Mix 5", "actions": [boost_off, mix5]}]}
    return deepcopy(project)
