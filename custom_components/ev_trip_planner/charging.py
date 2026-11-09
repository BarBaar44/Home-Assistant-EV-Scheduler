"""The charging side: publish the plan to evcc as a vehicle plan, and manage
the car's own charge limit, which evcc reads but will not raise.

Ported from the automation "evcc publish trip plan" (v0.2.0), which every
rule below comes from. The policy, in short:

* 80% (the normal limit) is the default. Above it only while a trip needs
  it.
* The limit comes back down once something positively says it is no longer
  needed: HA raised it and nothing needs it now, a charge actually reached
  it, or a booked trip needs no more than the normal limit. A floor plan is
  not a booked trip, so a limit set by hand holds until the charge that used
  it is done. An interrupted charge says nothing either.
* Never lowered while charging. Never written while the car's limit is
  unknown (a hand set limit above the plan would be overwritten).
* Every write is read back from the car before it is recorded. After the
  car slept the first command can fail with "Command signature/MAC is
  incorrect" (a stale signing session); the car is woken first and a write
  is tried up to three times.

Runs when the plan changes, every hour (a plan lost to an evcc restart
comes back), when the car stops charging, and once at start. A new run
replaces one still in progress, after a short delay that collapses bursts.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import LOGGER

if TYPE_CHECKING:
    from .coordinator import Plan, TripPlannerCoordinator

# Timing, module level so tests can shorten it.
DEBOUNCE_SECONDS = 10.0
WAKE_SECONDS = 20.0
CONFIRM_SECONDS = 15.0
POLL_SECONDS = 1.0
WRITE_TRIES = 3
REPUBLISH = dt.timedelta(hours=1)
# evcc cannot plan against "now": a later deadline is sent as now + this.
MIN_PLAN_AHEAD = dt.timedelta(minutes=15)
EVCC_TIMEOUT = aiohttp.ClientTimeout(total=10)

NOTE_LIMIT = "evcc_car_limit"
NOTE_PUBLISH = "evcc_plan_publish"
# The helper the automation used; read once to carry its state over.
LEGACY_RAISED = "input_boolean.evcc_car_limit_raised"

FIRED_PLAN = "plan"
FIRED_HOURLY = "hourly"
FIRED_STARTUP = "startup"
FIRED_CHARGE_ENDED = "charge_ended"


class ChargeController:
    """evcc plan publisher and car charge limit keeper for one planner."""

    def __init__(
        self, hass: HomeAssistant, coordinator: TripPlannerCoordinator
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._task: asyncio.Task | None = None
        self._last_plan: dict[str, Any] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ state

    @property
    def settings(self):
        return self.coordinator.settings

    @property
    def enabled(self) -> bool:
        s = self.settings
        return s.evcc_enabled or bool(s.car_limit_entity)

    @property
    def limit_raised(self) -> bool:
        """HA raised the car's limit above normal, and the car confirmed it."""
        return bool(self.coordinator.charging_state.get("limit_raised"))

    def _set_raised(self, value: bool) -> None:
        if self.limit_raised == value:
            return
        self.coordinator.charging_state["limit_raised"] = value
        self.coordinator.save()
        for listener in list(self._listeners):
            listener()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def adopt_legacy_state(self) -> None:
        """First run after the automation: carry its helper's state over, so
        a limit the automation raised is still restored silently."""
        state = self.coordinator.charging_state
        if "limit_raised" in state:
            return
        legacy = self.hass.states.get(LEGACY_RAISED)
        state["limit_raised"] = bool(legacy and legacy.state == "on")
        if state["limit_raised"]:
            LOGGER.info("Carried over %s: the car's limit was raised", LEGACY_RAISED)
            for listener in list(self._listeners):
                listener()
        self.coordinator.save()

    # ------------------------------------------------------------ setup

    @callback
    def async_start(self) -> None:
        if not self.enabled:
            return
        self.adopt_legacy_state()
        data = self.coordinator.data
        self._last_plan = asdict(data.plan) if data else None
        self._unsubs.append(self.coordinator.async_add_listener(self._plan_updated))
        self._unsubs.append(
            async_track_time_interval(
                self.hass, lambda _now: self.schedule(FIRED_HOURLY), REPUBLISH
            )
        )
        if self.settings.car_charging_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, self.settings.car_charging_entity, self._charging_changed
                )
            )
        if self.hass.state is CoreState.running:
            self.schedule(FIRED_STARTUP)
        else:
            self._unsubs.append(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED,
                    lambda _event: self.schedule(FIRED_STARTUP),
                )
            )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._task and not self._task.done():
            self._task.cancel()

    @callback
    def _plan_updated(self) -> None:
        """The coordinator updates listeners for every status and search
        change too: only a different plan is a reason to run."""
        data = self.coordinator.data
        plan = asdict(data.plan) if data else None
        if plan == self._last_plan:
            return
        self._last_plan = plan
        self.schedule(FIRED_PLAN)

    @callback
    def _charging_changed(self, event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if (
            old is not None
            and old.state == "charging"
            and new is not None
            and new.state not in ("charging", "unavailable", "unknown")
        ):
            self.schedule(FIRED_CHARGE_ENDED)

    @callback
    def schedule(self, fired_by: str) -> None:
        """Run after a short delay, replacing a run still in progress (the
        automation's mode: restart)."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = self.coordinator.config_entry.async_create_background_task(
            self.hass, self._delayed(fired_by), "ev_trip_planner charging"
        )

    async def _delayed(self, fired_by: str) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await self.async_run(fired_by)

    # -------------------------------------------------------------- run

    async def async_run(self, fired_by: str) -> None:
        """One pass: the car's limit first, then evcc, so this run's own
        publish (evcc contacting the car) does not come in between."""
        data = self.coordinator.data
        if data is None:
            return  # no plan known yet: an unknown plan is not an idle one
        plan = data.plan
        if self.settings.car_limit_entity:
            try:
                await self._car_limit(plan, fired_by)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never stop the publish
                LOGGER.exception("Charge limit step failed")
        if self.settings.evcc_enabled:
            await self._publish(plan)

    # ------------------------------------------------------- car limit

    def _read(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _limit(self) -> float | None:
        return self._read(self.settings.car_limit_entity)

    async def _wake(self) -> None:
        wake = self.settings.car_wake_entity
        if not wake:
            return
        try:
            await self.hass.services.async_call(
                "button", "press", {"entity_id": wake}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - the write may still work
            LOGGER.warning("Waking the car failed: %s", err)
        await asyncio.sleep(WAKE_SECONDS)

    async def _write(self, value: float, done: Callable[[float | None], bool]) -> bool:
        """Write the limit until the car reports `done`, at most WRITE_TRIES
        times, waiting for the car to confirm each write. A raise is never
        written over an unknown limit: a hand set one above the plan would
        be lowered. Lowering over an unknown limit is safe."""
        for attempt in range(WRITE_TRIES):
            current = self._limit()
            if done(current):
                return True
            if current is None and value > self.settings.normal_limit:
                # Raising over an unknown limit could lower a hand set one.
                return False
            try:
                await self.hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": self.settings.car_limit_entity, "value": value},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001 - retried, then reported
                LOGGER.warning(
                    "Charge limit write %d of %d failed: %s",
                    attempt + 1,
                    WRITE_TRIES,
                    err,
                )
            waited = 0.0
            while waited < CONFIRM_SECONDS and not done(self._limit()):
                await asyncio.sleep(POLL_SECONDS)
                waited += POLL_SECONDS
        return done(self._limit())

    async def _car_limit(self, plan: Plan, fired_by: str) -> None:
        s = self.settings
        normal = s.normal_limit
        soc = _plan_soc(plan)
        idle = _idle(plan)
        booked_trip = not idle and plan.kind == "trip"
        raise_needed = not idle and soc > normal
        # Unknown reads as 0 here, as in the automation: it wakes the car
        # for a raise and never qualifies for a lowering.
        car_limit = self._limit() or 0.0
        car_soc = self._read(s.soc_sensor) or 0.0
        charging = (
            self.hass.states.get(s.car_charging_entity)
            if s.car_charging_entity
            else None
        )
        is_charging = charging is not None and charging.state == "charging"

        if raise_needed and car_limit < soc:
            await self._wake()
            limit_awake = self._limit()
            if await self._write(soc, lambda v: v is not None and v >= soc):
                self._dismiss(NOTE_LIMIT)
                # Record and announce only a raise HA made. A limit already
                # high enough once the car woke (set by hand) stays a human's.
                if (
                    limit_awake is not None
                    and limit_awake < soc
                    and not self.limit_raised
                ):
                    self._set_raised(True)
                    await self._push(
                        plan,
                        "Tesla charge limit raised",
                        f"Your next trip needs {soc}%, so the charge limit is "
                        f"raised until then. It goes back to {normal:.0f}% "
                        f"afterwards.",
                    )
            else:
                self._note(
                    NOTE_LIMIT,
                    "Tesla charge limit not raised",
                    f"Could not raise the charge limit to {soc}% (car asleep or "
                    f"unreachable). Retrying hourly.",
                )
            return

        positively_not_needed = (
            self.limit_raised
            or (fired_by == FIRED_CHARGE_ENDED and car_soc >= car_limit - 1)
            or (booked_trip and soc <= normal)
        )
        if (
            not raise_needed
            and car_limit > normal
            and not is_charging
            and positively_not_needed
        ):
            await self._wake()
            # Unknown counts as not lowered yet: writing the normal limit
            # over an unknown one is safe.
            if await self._write(normal, lambda v: v is not None and v <= normal):
                if not self.limit_raised:
                    await self._push(
                        plan,
                        "Tesla charge limit reset",
                        f"The limit was {car_limit:.0f}% with nothing needing "
                        f"more than {normal:.0f}%, so it is back to {normal:.0f}%.",
                    )
                self._set_raised(False)
                self._dismiss(NOTE_LIMIT)
            else:
                self._note(
                    NOTE_LIMIT,
                    "Tesla charge limit not restored",
                    f"Could not lower the charge limit to {normal:.0f}% (car "
                    f"asleep or unreachable). Retrying hourly.",
                )

    # ------------------------------------------------------------ evcc

    async def _publish(self, plan: Plan) -> None:
        s = self.settings
        base = f"{s.evcc_url}/api/vehicles/{s.evcc_vehicle}/plan/soc"
        idle = _idle(plan)
        if idle:
            method, url = "DELETE", base
        else:
            when = max(
                plan.deadline or dt_util.utcnow(), dt_util.utcnow() + MIN_PLAN_AHEAD
            )
            # UTC with a Z keeps a "+" out of the URL path.
            stamp = dt_util.as_utc(when).strftime("%Y-%m-%dT%H:%M:%SZ")
            method, url = "POST", f"{base}/{_plan_soc(plan)}/{stamp}"
        session = async_get_clientsession(self.hass)
        try:
            async with session.request(method, url, timeout=EVCC_TIMEOUT) as resp:
                status: int | str = resp.status
        except (TimeoutError, aiohttp.ClientError) as err:
            LOGGER.warning("evcc %s %s failed: %s", method, url, err)
            status = "no response"
        if isinstance(status, int) and status < 300:
            self._dismiss(NOTE_PUBLISH)
            return
        LOGGER.warning("evcc %s %s answered %s", method, url, status)
        self._note(
            NOTE_PUBLISH,
            "evcc plan not updated",
            f"Could not {'clear' if idle else 'set'} the charging plan in evcc "
            f"({status}).",
        )

    # ---------------------------------------------------------- notices

    async def _push(self, plan: Plan, title: str, message: str) -> None:
        notify = plan.notify_service or self.settings.default_notify
        domain, _, service = notify.partition(".")
        try:
            await self.hass.services.async_call(
                domain, service, {"title": title, "message": message}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - a push is never fatal
            LOGGER.warning("Push via %s failed: %s", notify, err)

    def _note(self, note_id: str, title: str, message: str) -> None:
        persistent_notification.async_create(self.hass, message, title, note_id)

    def _dismiss(self, note_id: str) -> None:
        persistent_notification.async_dismiss(self.hass, note_id)


def _idle(plan: Plan) -> bool:
    return plan.kind in (None, "idle") or plan.soc <= 0


def _plan_soc(plan: Plan) -> int:
    """Whole percent, rounded up, at most 100."""
    return min(math.ceil(plan.soc), 100)
