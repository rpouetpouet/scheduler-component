#!/usr/bin/env python
"""Test de la logique de declenchement du composant Scheduler avec `weekdays` par creneau.

On importe le VRAI timer.py / const.py du composant (seul `homeassistant` et `store`
sont simules), et on verifie calculate_timestamp / next_timeslot / current_timeslot
sur les 7 jours de la semaine.

Lancement :  cd ~/ha-forks && .venv-test/bin/python component-tests/test_slot_weekdays.py
"""
import datetime as dt
import importlib
import sys
import types
import zoneinfo
from pathlib import Path

COMPONENT_DIR = Path("/home/batman/ha-forks/scheduler-component/custom_components/scheduler")
TZ = zoneinfo.ZoneInfo("Europe/Paris")

# ---------------------------------------------------------------- stubs Home Assistant
CLOCK = {"now": None}  # datetime aware, heure locale


def utcnow():
    return CLOCK["now"].astimezone(dt.timezone.utc)


def as_local(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def parse_time(value):
    if isinstance(value, dt.time):
        return value
    h, m, *rest = str(value).split(":")
    return dt.time(int(h), int(m), int(rest[0]) if rest else 0)


def parse_datetime(value):
    return dt.datetime.fromisoformat(value)


def find_next_time_expression_time(now, second, minute, hour):
    """version simplifiee mais fidele a la semantique HA : strictement apres `now`"""
    second, minute, hour = set(second), set(minute), set(hour)
    candidate = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(60 * 24 * 9):
        if candidate.second in second and candidate.minute in minute and candidate.hour in hour:
            return candidate
        candidate += dt.timedelta(minutes=1)
    raise RuntimeError("find_next_time_expression_time: introuvable")


def make_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


ha = make_module("homeassistant")
ha_util = make_module("homeassistant.util")
ha_dt = make_module(
    "homeassistant.util.dt",
    utcnow=utcnow,
    as_local=as_local,
    parse_time=parse_time,
    parse_datetime=parse_datetime,
    find_next_time_expression_time=find_next_time_expression_time,
)
ha_util.dt = ha_dt
ha_const = make_module(
    "homeassistant.const",
    WEEKDAYS=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    STATE_ON="on",
    STATE_OFF="off",
    ATTR_ENTITY_ID="entity_id",
    ATTR_SERVICE="service",
    ATTR_SERVICE_DATA="service_data",
    ATTR_NAME="name",
    SUN_EVENT_SUNRISE="sunrise",
    SUN_EVENT_SUNSET="sunset",
    CONF_CONDITIONS="conditions",
    CONF_ATTRIBUTE="attribute",
)
ha_core = make_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
ha_helpers = make_module("homeassistant.helpers")
ha_cv = make_module(
    "homeassistant.helpers.config_validation",
    boolean=lambda v: v,
    string=lambda v: v,
    entity_id=lambda v: v,
    ensure_list=lambda v: v if isinstance(v, list) else [v],
)
ha_event = make_module(
    "homeassistant.helpers.event",
    async_track_point_in_time=lambda *a, **k: (lambda: None),
    async_track_state_change_event=lambda *a, **k: (lambda: None),
)
ha_dispatcher = make_module(
    "homeassistant.helpers.dispatcher",
    async_dispatcher_connect=lambda *a, **k: (lambda: None),
    async_dispatcher_send=lambda *a, **k: None,
)
ha_helpers.config_validation = ha_cv
ha_helpers.event = ha_event
ha_helpers.dispatcher = ha_dispatcher

# ------------------------------------------------------------- paquet du composant
pkg = types.ModuleType("sched")
pkg.__path__ = [str(COMPONENT_DIR)]
sys.modules["sched"] = pkg
store_stub = types.ModuleType("sched.store")
store_stub.async_get_registry = lambda hass: None


class _Entry(dict):
    pass


store_stub.ScheduleEntry = _Entry
sys.modules["sched.store"] = store_stub

const = importlib.import_module("sched.const")
timer = importlib.import_module("sched.timer")
TimerHandler = timer.TimerHandler


# ------------------------------------------------------------------- faux hass
class FakeState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class FakeStates:
    def __init__(self):
        self._states = {}

    def get(self, entity_id):
        return self._states.get(entity_id)


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.loop = types.SimpleNamespace(create_task=lambda coro: None)
        self.data = {const.DOMAIN: {}}


def make_handler(timeslots, weekdays=None):
    """construit un TimerHandler sans passer par le store (données injectées)"""
    handler = TimerHandler.__new__(TimerHandler)
    handler.hass = FakeHass()
    handler.id = "test"
    handler._weekdays = list(weekdays or [const.DAY_TYPE_DAILY])
    handler._start_date = None
    handler._end_date = None
    handler._timeslots = [dict(slot) for slot in timeslots]
    handler._timer = None
    handler._next_trigger = None
    handler._next_slot = None
    handler._sun_tracker = None
    handler._workday_tracker = None
    handler._watched_times = []
    handler.slot_queue = []
    handler.timestamps = []
    handler.current_slot = None
    return handler


def at(year, month, day, hour, minute):
    CLOCK["now"] = dt.datetime(year, month, day, hour, minute, tzinfo=TZ)


# --------------------------------------------------------- planning "clim filles"
# un SEUL schedule : 9 creneaux contigus, chacun avec ses propres jours
SEM = ["mon", "tue", "thu", "fri"]
WED = ["wed"]
WE = ["sat", "sun"]

FILLES = [
    {"start": "00:00:00", "stop": "05:15:00", "value": 19, "weekdays": None},
    {"start": "05:15:00", "stop": "06:45:00", "value": 21, "weekdays": None},
    {"start": "06:45:00", "stop": "07:30:00", "value": 19, "weekdays": SEM},
    {"start": "07:30:00", "stop": "12:00:00", "value": 19, "weekdays": WED},
    {"start": "12:00:00", "stop": "17:30:00", "value": 21, "weekdays": WED},
    {"start": "17:30:00", "stop": "20:45:00", "value": 21, "weekdays": SEM},
    {"start": "20:45:00", "stop": "21:00:00", "value": 19, "weekdays": SEM},
    {"start": "21:00:00", "stop": "21:30:00", "value": 19, "weekdays": WED},
    {"start": "21:30:00", "stop": "00:00:00", "value": 19, "weekdays": WE},
]


def slots_of(spec):
    out = []
    for slot in spec:
        entry = {const.ATTR_START: slot["start"], const.ATTR_STOP: slot["stop"]}
        if slot["weekdays"]:
            entry[const.ATTR_WEEKDAYS] = slot["weekdays"]
        out.append(entry)
    return out


results = []


def check(name, got, expected):
    ok = got == expected
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  got={got!r} expected={expected!r}")


# reference : 2026-11-02 est un lundi
for label, date in [
    ("lundi", (2026, 11, 2)),
    ("mardi", (2026, 11, 3)),
    ("mercredi", (2026, 11, 4)),
    ("jeudi", (2026, 11, 5)),
    ("vendredi", (2026, 11, 6)),
    ("samedi", (2026, 11, 7)),
    ("dimanche", (2026, 11, 8)),
]:
    at(date[0], date[1], date[2], 0, 0)
    print(f"-- reference {label}: {CLOCK['now'].strftime('%A %d/%m/%Y')}")

# 1) indépendance des jours : ce que le timer considère comme "prochain créneau"
cases = [
    # (date, heure, attendu: (index, start))
    ((2026, 11, 2, 6, 0), (2, "2026-11-02 06:45")),   # lundi  -> creneau SEM 06:45
    ((2026, 11, 3, 6, 0), (2, "2026-11-03 06:45")),   # mardi  -> creneau SEM 06:45
    ((2026, 11, 4, 6, 0), (3, "2026-11-04 07:30")),   # mercredi -> 06:45 ne s'applique pas
    ((2026, 11, 5, 6, 0), (2, "2026-11-05 06:45")),   # jeudi
    ((2026, 11, 6, 6, 0), (2, "2026-11-06 06:45")),   # vendredi
    ((2026, 11, 7, 6, 0), (8, "2026-11-07 21:30")),   # samedi -> pas de 06:45, pas de 07:30
    ((2026, 11, 8, 22, 0), (0, "2026-11-09 00:00")),  # dimanche 22h -> 00:00 le lendemain
    ((2026, 11, 4, 11, 0), (4, "2026-11-04 12:00")),  # mercredi 11h -> confort 12:00
    ((2026, 11, 4, 13, 0), (7, "2026-11-04 21:00")),  # mercredi 13h -> eco 21:00
    ((2026, 11, 3, 13, 0), (5, "2026-11-03 17:30")),  # mardi 13h -> confort 17:30
]
for (y, mo, d, h, mi), (expected_idx, expected_ts) in cases:
    at(y, mo, d, h, mi)
    handler = make_handler(slots_of(FILLES))
    index, timestamp = handler.next_timeslot()
    got = (index, timestamp.strftime("%Y-%m-%d %H:%M") if timestamp else None)
    check(f"next_timeslot {CLOCK['now'].strftime('%a %d/%m %H:%M')}", got, (expected_idx, expected_ts))

# 2) créneau courant : un créneau qui ne s'applique pas aujourd'hui n'est jamais "actif"
at(2026, 11, 4, 6, 50)  # mercredi : creneau 06:45-07:30 est SEM uniquement
handler = make_handler(slots_of(FILLES))
check("current_timeslot mercredi 06:50 (creneau SEM) -> aucun", handler.current_timeslot(), (None, None))

at(2026, 11, 3, 6, 50)  # mardi : le meme creneau s'applique
handler = make_handler(slots_of(FILLES))
idx, stop = handler.current_timeslot()
check(
    "current_timeslot mardi 06:50 -> creneau 2, fin 07:30",
    (idx, stop.strftime("%H:%M") if stop else None),
    (2, "07:30"),
)

# 3) rétro-compatibilité : aucun creneau n'a de jours -> comportement d'avant
legacy = [{"start": "06:45:00", "stop": "07:30:00", "value": 19, "weekdays": None}]
for (y, mo, d), label in [((2026, 11, 4), "mercredi"), ((2026, 11, 3), "mardi")]:
    at(y, mo, d, 6, 0)
    handler = make_handler(slots_of(legacy))
    idx, timestamp = handler.next_timeslot()
    check(
        f"retro-compat sans weekdays ({label} 06:00 -> 06:45)",
        (idx, timestamp.strftime("%H:%M") if timestamp else None),
        (0, "06:45"),
    )

# 4) day type "weekend" porte par un creneau seul
we_slot = [{"start": "21:30:00", "stop": "00:00:00", "value": 19, "weekdays": [const.DAY_TYPE_WEEKEND]}]
at(2026, 11, 7, 20, 0)  # samedi
handler = make_handler(slots_of(we_slot))
idx, timestamp = handler.next_timeslot()
check("weekend via DAY_TYPE_WEEKEND (samedi 20:00)", (idx, timestamp.strftime("%H:%M") if timestamp else None), (0, "21:30"))

at(2026, 11, 3, 20, 0)  # mardi
handler = make_handler(slots_of(we_slot))
idx, timestamp = handler.next_timeslot()
check(
    "weekend via DAY_TYPE_WEEKEND (mardi 20:00 -> samedi 21:30)",
    (idx, timestamp.strftime("%a %d %H:%M") if timestamp else None),
    (0, "Sat 07 21:30"),
)

# 5) validation du schema de l'API : le champ est accepte, les valeurs invalides rejetees
import voluptuous as vol

valid_payload = {
    "weekdays": ["daily"],
    "repeat_type": "repeat",
    "timeslots": [
        {"start": "06:45:00", "stop": "07:30:00", "actions": [{"service": "script.x"}]},
        {
            "start": "07:30:00",
            "stop": "12:00:00",
            "weekdays": ["wed"],
            "actions": [{"service": "script.y"}],
        },
    ],
}
try:
    const.ADD_SCHEDULE_SCHEMA(valid_payload)
    check("schema API accepte un timeslot avec weekdays", True, True)
except vol.Invalid as exc:
    check("schema API accepte un timeslot avec weekdays", False, f"Invalid: {exc}")

bad_payload = {
    "weekdays": ["daily"],
    "repeat_type": "repeat",
    "timeslots": [
        {
            "start": "07:30:00",
            "weekdays": ["funday"],
            "actions": [{"service": "script.y"}],
        }
    ],
}
try:
    const.ADD_SCHEDULE_SCHEMA(bad_payload)
    check("schema API rejette un jour invalide", False, "aucune exception")
except vol.Invalid:
    check("schema API rejette un jour invalide", True, True)

legacy_payload = {
    "weekdays": ["mon", "tue"],
    "repeat_type": "repeat",
    "timeslots": [{"start": "06:45:00", "actions": [{"service": "script.x"}]}],
}
try:
    parsed = const.ADD_SCHEDULE_SCHEMA(legacy_payload)
    check(
        "schema API accepte un payload legacy (sans weekdays de creneau)",
        "weekdays" not in parsed["timeslots"][0],
        True,
    )
except vol.Invalid as exc:
    check("schema API accepte un payload legacy", False, f"Invalid: {exc}")

failed = results.count(False)
print(f"\n{len(results) - failed}/{len(results)} tests OK")
sys.exit(1 if failed else 0)
