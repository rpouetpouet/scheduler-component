#!/usr/bin/env python
"""Test de non-regression : un schedule avec `weekdays` par creneau doit survivre
a l'ecriture du storage (bug trouve le 04/11/2026 : _data_to_save reconstruisait
chaque creneau avec une liste de cles explicite et jetait `weekdays`).

Lancement : cd ~/ha-forks && .venv-test/bin/python component-tests/test_store_roundtrip.py
"""
import importlib
import sys
import types
from pathlib import Path

COMPONENT_DIR = Path("/home/batman/ha-forks/scheduler-component/custom_components/scheduler")


def make_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Store:  # homeassistant.helpers.storage.Store
    def __init__(self, *args, **kwargs):
        pass

    async def async_load(self):
        return None


make_module("homeassistant")
make_module("homeassistant.util")
make_module("homeassistant.util.dt")
make_module("homeassistant.core", callback=lambda f: f, HomeAssistant=object)
make_module("homeassistant.loader", bind_hass=lambda f: f)
make_module(
    "homeassistant.const",
    ATTR_ENTITY_ID="entity_id",
    ATTR_NAME="name",
    CONF_CONDITIONS="conditions",
    WEEKDAYS=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    STATE_ON="on",
    STATE_OFF="off",
    SUN_EVENT_SUNRISE="sunrise",
    SUN_EVENT_SUNSET="sunset",
    ATTR_SERVICE="service",
    ATTR_SERVICE_DATA="service_data",
    CONF_ATTRIBUTE="attribute",
)
make_module("homeassistant.helpers")
make_module("homeassistant.helpers.storage", Store=_Store)
make_module(
    "homeassistant.helpers.config_validation",
    boolean=lambda v: v,
    string=lambda v: v,
    entity_id=lambda v: v,
    ensure_list=lambda v: v if isinstance(v, list) else [v],
)

pkg = types.ModuleType("sched")
pkg.__path__ = [str(COMPONENT_DIR)]
sys.modules["sched"] = pkg

const = importlib.import_module("sched.const")
store = importlib.import_module("sched.store")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")


# schedule tel que l'API le recoit (payload de /api/scheduler/add)
payload = {
    "weekdays": ["daily"],
    "repeat_type": "repeat",
    "name": "Filles",
    "start_date": None,
    "end_date": None,
    "timeslots": [
        {"start": "00:00:00", "stop": "05:15:00", "actions": [{"service": "script.eco"}]},
        {
            "start": "06:45:00",
            "stop": "07:30:00",
            "weekdays": ["mon", "tue", "thu", "fri"],
            "actions": [{"service": "script.eco"}],
        },
        {
            "start": "21:30:00",
            "stop": "00:00:00",
            "weekdays": ["sat", "sun"],
            "actions": [{"service": "script.eco"}],
        },
    ],
}

parsed = store.parse_schedule_data(dict(payload))
entry = store.ScheduleEntry(**parsed, schedule_id="abc123")

check("parse_schedule_data conserve weekdays du creneau 1", entry.timeslots[1].weekdays == ["mon", "tue", "thu", "fri"], repr(entry.timeslots[1].weekdays))
check("creneau sans restriction -> weekdays None", entry.timeslots[0].weekdays is None, repr(entry.timeslots[0].weekdays))

# ecriture du storage
storage = store.ScheduleStorage.__new__(store.ScheduleStorage)
storage.schedules = {"abc123": entry}
storage.tags = {}
storage.time_shutdown = None

saved = storage._data_to_save()
slots = saved["schedules"][0]["timeslots"]

check(
    "ecriture : le creneau 1 garde weekdays (regression du 04/11/2026)",
    slots[1].get("weekdays") == ["mon", "tue", "thu", "fri"],
    repr(slots[1].get("weekdays")),
)
check("ecriture : creneau 0 sans weekdays", slots[0].get("weekdays") is None, repr(slots[0].get("weekdays")))
check(
    "ecriture : creneau week-end garde weekdays",
    slots[2].get("weekdays") == ["sat", "sun"],
    repr(slots[2].get("weekdays")),
)

# relecture apres redemarrage : le storage est repasse dans parse_schedule_data
reloaded = store.parse_schedule_data({"timeslots": slots})
check(
    "relecture apres restart : weekdays toujours la",
    reloaded["timeslots"][1].weekdays == ["mon", "tue", "thu", "fri"],
    repr(reloaded["timeslots"][1].weekdays),
)

failed = results.count(False)
print(f"\n{len(results) - failed}/{len(results)} tests OK")
sys.exit(1 if failed else 0)
