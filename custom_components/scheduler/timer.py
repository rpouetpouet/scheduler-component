import logging
import datetime


import homeassistant.util.dt as dt_util
from homeassistant.const import (
    WEEKDAYS,
    STATE_ON,
    STATE_OFF,
)
from homeassistant.core import (
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)


from . import const
from .store import async_get_registry

_LOGGER = logging.getLogger(__name__)

ATTR_NEXT_RISING = "next_rising"
ATTR_NEXT_SETTING = "next_setting"
ATTR_WORKDAYS = "workdays"


def has_sun(time_str: str):
    return const.OffsetTimePattern.match(time_str)


def is_same_day(dateA: datetime.datetime, dateB: datetime.datetime):
    return dateA.date() == dateB.date()


def days_until_date(date_string: str, ts: datetime.datetime):
    date = dt_util.parse_date(date_string)
    diff = date - ts.date()
    return diff.days


def find_closest_from_now(date_arr: list):
    now = dt_util.as_local(dt_util.utcnow())
    minimum = None
    for item in date_arr:
        if item is not None:
            if minimum is None:
                minimum = item
            elif item > now:
                if item < minimum or minimum < now:
                    minimum = item
            else:
                if item < minimum and minimum < now:
                    minimum = item
    return minimum


class TimerHandler:
    def __init__(self, hass: HomeAssistant, id: str):
        """init"""
        self.hass = hass
        self.id = id
        self._weekdays = []
        self._start_date = None
        self._end_date = None
        self._timeslots = []
        self._timer = None
        self._next_trigger = None
        self._next_slot = None
        self._sun_tracker = None
        self._workday_tracker = None
        self._watched_times = []

        self.slot_queue = []
        self.timestamps = []
        self.current_slot = None

        self.hass.loop.create_task(self.async_reload_data())

        @callback
        async def async_item_updated(id: str):
            if id == self.id:
                await self.async_reload_data()

        self._update_listener = async_dispatcher_connect(
            self.hass, const.EVENT_ITEM_UPDATED, async_item_updated
        )

    async def async_reload_data(self):
        """load schedule data into timer class object and start timer"""
        store = await async_get_registry(self.hass)
        data = store.async_get_schedule(self.id)

        self._weekdays = data[const.ATTR_WEEKDAYS]
        self._start_date = data[const.ATTR_START_DATE]
        self._end_date = data[const.ATTR_END_DATE]
        self._timeslots = [
            dict(
                (k, slot[k])
                for k in [const.ATTR_START, const.ATTR_STOP, const.ATTR_WEEKDAYS]
                if k in slot
            )
            for slot in data[const.ATTR_TIMESLOTS]
        ]
        await self.async_start_timer()

    async def async_unload(self):
        """unload a timer class object"""
        await self.async_stop_timer()
        self._update_listener()
        self._next_trigger = None

    async def async_start_timer(self):
        [current_slot, timestamp_end] = self.current_timeslot()
        [next_slot, timestamp_next] = self.next_timeslot()

        self._watched_times = []
        if timestamp_next is not None:
            self._watched_times.append(self._timeslots[next_slot][const.ATTR_START])
        if timestamp_end is not None:
            self._watched_times.append(self._timeslots[current_slot][const.ATTR_STOP])

        # the next trigger time is next slot or end of current slot (whichever comes first)
        timestamp = find_closest_from_now([timestamp_end, timestamp_next])
        self._timer_is_endpoint = (
            timestamp != timestamp_next and timestamp == timestamp_end
        )
        if timestamp == timestamp_next and timestamp is not None:
            self._next_slot = next_slot
        else:
            self._next_slot = None

        self.current_slot = current_slot
        self._next_trigger = timestamp

        await self.async_start_sun_tracker()
        now = dt_util.as_local(dt_util.utcnow())

        if timestamp is not None:
            if self._timer:
                self._timer()

            if (timestamp - now).total_seconds() < 0:
                self._timer = None
                _LOGGER.debug(
                    "Timer of {} is not set because it is in the past ({})".format(
                        self.id, timestamp
                    )
                )
            else:
                self._timer = async_track_point_in_time(
                    self.hass, self.async_timer_finished, timestamp
                )
                _LOGGER.debug("Timer of {} set for {}".format(self.id, timestamp))
                await self.async_start_workday_tracker()

        async_dispatcher_send(self.hass, const.EVENT_TIMER_UPDATED, self.id)

    async def async_stop_timer(self):
        """stop the timer"""
        if self._timer:
            self._timer()
            self._timer = None
        await self.async_stop_sun_tracker()
        await self.async_stop_workday_tracker()

    async def async_start_sun_tracker(self):
        """check for changes in the sun sensor"""
        if (
            self._next_trigger is not None
            and any(has_sun(x) for x in self._watched_times)
        ) or (
            self._next_trigger is None
            and all(has_sun(x[const.ATTR_START]) for x in self._timeslots)
        ):
            # install sun tracker for updating timer when sun changes
            # initially the time calculation may fail due to the sun entity being unavailable

            if self._sun_tracker is not None:
                # the tracker is already running
                return

            @callback
            async def async_sun_updated(_event):
                """the sun entity was updated"""
                # sun entity changed
                if self._next_trigger is None:
                    # sun entity has initialized
                    await self.async_start_timer()
                    return
                ts = find_closest_from_now(
                    self.calculate_timestamp(x) for x in self._watched_times
                )
                if not ts or not self._next_trigger:
                    # sun entity became unavailable (or other corner case)
                    await self.async_start_timer()
                    return
                # we are re-scheduling an existing timer
                delta = (ts - self._next_trigger).total_seconds()
                if abs(delta) >= 60 and abs(delta) < 2000:
                    # only reschedule if the difference is at least a minute
                    # only reschedule if this doesnt cause the timer to shift to another day (+/- 24 hrs delta)
                    # only reschedule if this doesnt cause the timer to shift to another hour (due to DST change)
                    await self.async_start_timer()

            self._sun_tracker = async_track_state_change_event(
                self.hass, const.SUN_ENTITY, async_sun_updated
            )
        else:
            # clear existing tracker
            await self.async_stop_sun_tracker()

    async def async_stop_sun_tracker(self):
        """stop checking for changes in the sun sensor"""
        if self._sun_tracker:
            self._sun_tracker()
            self._sun_tracker = None

    async def async_start_workday_tracker(self):
        """check for changes in the workday sensor"""
        if (
            self.has_day_type(const.DAY_TYPE_WORKDAY)
            or self.has_day_type(const.DAY_TYPE_WEEKEND)
        ):
            # install tracker for updating timer when workday sensor changes

            if self._workday_tracker is not None:
                # the tracker is already running
                return

            @callback
            async def async_workday_updated():
                """the workday sensor was updated"""
                [current_slot, timestamp_end] = self.current_timeslot()
                [next_slot, timestamp_next] = self.next_timeslot()
                ts_next = find_closest_from_now([timestamp_end, timestamp_next])

                # workday entity changed
                if not ts_next or not self._next_trigger:
                    # timer was not yet set
                    await self.async_start_timer()
                else:
                    # we are re-scheduling an existing timer
                    delta = (ts_next - self._next_trigger).total_seconds()
                    if abs(delta) >= 60:
                        # only reschedule if the difference is at least a minute
                        await self.async_start_timer()

            self._workday_tracker = async_dispatcher_connect(
                self.hass, const.EVENT_WORKDAY_SENSOR_UPDATED, async_workday_updated
            )
        else:
            # clear existing tracker
            await self.async_stop_workday_tracker()

    async def async_stop_workday_tracker(self):
        """stop checking for changes in the workday sensor"""
        if self._workday_tracker:
            self._workday_tracker()
            self._workday_tracker = None

    async def async_timer_finished(self, _time):
        """the timer is finished"""
        if not self._timer_is_endpoint:
            # timer marks the start of a new timeslot
            self.current_slot = self._next_slot
            _LOGGER.debug(
                "Timer {} has reached slot {}".format(self.id, self.current_slot)
            )
            async_dispatcher_send(self.hass, const.EVENT_TIMER_FINISHED, self.id)
            # don't automatically reset, wait for external reset after 1 minute
            # await self.async_start_timer()
            await self.async_stop_timer()
        else:
            # timer marks the end of a timeslot
            _LOGGER.debug(
                "Timer {} has reached end of timeslot, resetting..".format(self.id)
            )
            await self.async_start_timer()

    def slot_weekdays(self, slot: dict) -> list:
        """the days a timeslot applies to (its own list, or the ones of the schedule)"""
        weekdays = slot.get(const.ATTR_WEEKDAYS)
        return weekdays if weekdays else self._weekdays

    def has_day_type(self, day_type: str) -> bool:
        """whether the schedule or one of its timeslots uses a day type (workday/weekend)"""
        if day_type in self._weekdays:
            return True
        return any(day_type in self.slot_weekdays(slot) for slot in self._timeslots)

    def day_in_weekdays(
        self, ts: datetime.datetime, weekdays: list = None
    ) -> bool:
        """check if the day of a datetime object is in the allowed list of days"""
        weekdays = self._weekdays if weekdays is None else weekdays
        day = WEEKDAYS[ts.weekday()]
        workday_sensor = self.hass.states.get(const.WORKDAY_ENTITY)

        if (
            workday_sensor
            and workday_sensor.state in [STATE_ON, STATE_OFF]
            and is_same_day(ts, dt_util.as_local(dt_util.utcnow()))
        ):
            # state of workday sensor is used for evaluating workday vs weekend
            if const.DAY_TYPE_WORKDAY in weekdays:
                return workday_sensor.state == STATE_ON
            elif const.DAY_TYPE_WEEKEND in weekdays:
                return workday_sensor.state == STATE_OFF

        if workday_sensor and ATTR_WORKDAYS in workday_sensor.attributes:
            # workday sensor defines a list of workdays
            workday_list = workday_sensor.attributes[ATTR_WORKDAYS]
            weekend_list = [e for e in WEEKDAYS if e not in workday_list]
        else:
            # assume workdays are mon-fri
            workday_list = WEEKDAYS[0:5]
            weekend_list = WEEKDAYS[5:7]

        if const.DAY_TYPE_DAILY in weekdays or not len(weekdays):
            return True
        elif const.DAY_TYPE_WORKDAY in weekdays and day in workday_list:
            return True
        elif const.DAY_TYPE_WEEKEND in weekdays and day in weekend_list:
            return True
        return day in weekdays

    def calculate_timestamp(
        self,
        time_str,
        now: datetime.datetime = None,
        iteration: int = 0,
        reverse_direction: bool = False,
        weekdays: list = None,
    ) -> datetime.datetime:
        """calculate the next occurence of a time string"""
        if time_str is None:
            return None
        if now is None:
            now = dt_util.as_local(dt_util.utcnow())

        res = has_sun(time_str)
        if not res:
            # fixed time
            time = dt_util.parse_time(time_str)
            ts = dt_util.find_next_time_expression_time(
                now, [time.second], [time.minute], [time.hour]
            )
        else:
            # relative to sunrise/sunset
            sun = self.hass.states.get(const.SUN_ENTITY)
            if not sun:
                return None
            ts = None
            if (
                res.group(1) == const.SUN_EVENT_SUNRISE
                and ATTR_NEXT_RISING in sun.attributes
            ):
                ts = dt_util.parse_datetime(sun.attributes[ATTR_NEXT_RISING])
            elif (
                res.group(1) == const.SUN_EVENT_SUNSET
                and ATTR_NEXT_SETTING in sun.attributes
            ):
                ts = dt_util.parse_datetime(sun.attributes[ATTR_NEXT_SETTING])
            if not ts:
                return None
            ts = dt_util.as_local(ts)
            ts = ts.replace(second=0)
            time_sun = datetime.timedelta(
                hours=ts.hour, minutes=ts.minute, seconds=ts.second
            )
            offset = dt_util.parse_time(res.group(3))
            offset = datetime.timedelta(
                hours=offset.hour, minutes=offset.minute, seconds=offset.second
            )
            if res.group(2) == "-":
                if (time_sun - offset).total_seconds() >= 0:
                    ts = ts - offset
                else:
                    # prevent offset to shift the time past the extends of the day
                    ts = ts.replace(hour=0, minute=0, second=0)
            else:
                if (time_sun + offset).total_seconds() <= 86340:
                    ts = ts + offset
                else:
                    # prevent offset to shift the time past the extends of the day
                    ts = ts.replace(hour=23, minute=59, second=0)
            ts = dt_util.find_next_time_expression_time(
                now, [ts.second], [ts.minute], [ts.hour]
            )

        time_delta = datetime.timedelta(seconds=1)

        if self.day_in_weekdays(ts, weekdays) and (
            (ts - now).total_seconds() > 0 or iteration > 0
        ):

            if self._start_date and days_until_date(self._start_date, ts) > 0:
                # start date is in the future, jump to start date
                end_of_day = ts.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
                days_delta = days_until_date(self._start_date, end_of_day)
                if days_delta:
                    time_delta = datetime.timedelta(days=days_delta)

            elif self._end_date and days_until_date(self._end_date, ts) < 0:
                # end date is in the past, jump to end date
                time_delta = datetime.timedelta(
                    days=days_until_date(self._end_date, ts)
                )
                reverse_direction = True

            else:
                # date restrictions are met
                return ts
        elif reverse_direction:
            time_delta = datetime.timedelta(days=-1)

        # calculate next timestamp
        next_day = dt_util.find_next_time_expression_time(
            now + time_delta, [0], [0], [0]
        )
        if iteration > 15:
            _LOGGER.warning(
                "failed to calculate next timeslot for schedule {}".format(self.id)
            )
            return None
        return self.calculate_timestamp(
            time_str, next_day, iteration + 1, reverse_direction, weekdays
        )

    def next_timeslot(self):
        """calculate the closest timeslot from now"""
        now = dt_util.as_local(dt_util.utcnow())
        # calculate next start of all timeslots
        timestamps = [
            self.calculate_timestamp(
                slot[const.ATTR_START], now, weekdays=self.slot_weekdays(slot)
            )
            for slot in self._timeslots
        ]

        # calculate timeslot that will start soonest (or closest in the past)
        remaining = [
            (ts - now).total_seconds() if ts is not None else now.timestamp()
            for ts in timestamps
        ]
        # prefer upcoming timeslots over past ones: with an end_date of today, a timeslot that
        # already passed today is returned in the past and could hide the next timeslot
        slot_order = sorted(
            range(len(remaining)), key=lambda k: (remaining[k] < 0, abs(remaining[k]))
        )

        # filter out timeslots that cannot be computed
        for i in range(len(timestamps)):
            if timestamps[i] is None:
                slot_order.remove(i)
        timestamps = [e for e in timestamps if e is not None]

        self.slot_queue = slot_order
        self.timestamps = timestamps

        next_slot = slot_order[0] if len(slot_order) > 0 else None

        return (next_slot, timestamps[next_slot] if next_slot is not None else None)

    def current_timeslot(self, now: datetime.datetime = None):
        """calculate the end of the timeslot that is overlapping now"""
        if now is None:
            now = dt_util.as_local(dt_util.utcnow())

        def unwrap_end_of_day(time_str: str):
            if time_str == "00:00:00":
                return "23:59:59"
            else:
                return time_str

        # calculate next stop of all timeslots that apply to the current day
        candidates = []
        for idx, slot in enumerate(self._timeslots):
            weekdays = self.slot_weekdays(slot)
            if not self.day_in_weekdays(now, weekdays):
                # timeslot does not run today, it has no active period
                continue
            if slot[const.ATTR_STOP] is not None:
                ts = self.calculate_timestamp(
                    unwrap_end_of_day(slot[const.ATTR_STOP]), now, weekdays=weekdays
                )
            else:
                ts = self.calculate_timestamp(
                    slot[const.ATTR_START], now, weekdays=weekdays
                )
                if ts is not None:
                    ts = ts + datetime.timedelta(minutes=1)
                    ts = self.calculate_timestamp(
                        ts.strftime("%H:%M:%S"), now, weekdays=weekdays
                    )
            if ts is None:
                continue
            candidates.append((idx, ts))

        if not candidates:
            return (None, None)

        # calculate timeslot that will end soonest
        (next_slot_end, stop) = sorted(
            candidates,
            key=lambda item: (
                (item[1] - now).total_seconds() < 0,
                abs((item[1] - now).total_seconds()),
            ),
        )[0]
        if stop is not None:
            # calculate last start of timeslot that will end soonest
            if (stop - now).total_seconds() < 0:
                # end of timeslot is in the past
                return (None, None)

            start = self.calculate_timestamp(
                self._timeslots[next_slot_end][const.ATTR_START],
                stop - datetime.timedelta(days=1),
                weekdays=self.slot_weekdays(self._timeslots[next_slot_end]),
            )

            if start is not None:
                elapsed = (now - start).total_seconds()
                if elapsed > 0:
                    # timeslot is currently overlapping
                    return (
                        next_slot_end,
                        stop
                        if self._timeslots[next_slot_end][const.ATTR_STOP] is not None
                        else None,
                    )
        return (None, None)
