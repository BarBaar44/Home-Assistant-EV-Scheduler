"""The charging side: evcc plan publisher and the car's charge limit.

One test per row of the charge limit policy (claude/Charging_control.md,
"The car charge limit policy"), ported from the automation it replaces."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.ev_trip_planner import charging
from custom_components.ev_trip_planner.coordinator import Plan

from .conftest import make_entry

LIMIT = "number.car_limit"
WAKE = "button.car_wake"
CHARGING = "sensor.car_charging"
SOC = "sensor.car_battery"
EVCC = "http://evcc.local:7070"
PLAN_URL = re.compile(rf"{re.escape(EVCC)}/api/vehicles/tesla/plan/soc.*")


@dataclass
class FakeCar:
    hass: HomeAssistant
    confirm: bool = True  # the car takes a write
    fail_writes: int = 0  # this many writes raise first (stale session)
    writes: list[float] = field(default_factory=list)
    wakes: int = 0

    def set(self, limit: Any, soc: float = 50, charging_state: str = "stopped"):
        self.hass.states.async_set(LIMIT, str(limit))
        self.hass.states.async_set(SOC, str(soc))
        self.hass.states.async_set(CHARGING, charging_state)


@pytest.fixture(autouse=True)
def fast() -> Generator[None]:
    with (
        patch.object(charging, "DEBOUNCE_SECONDS", 0),
        patch.object(charging, "WAKE_SECONDS", 0),
        patch.object(charging, "CONFIRM_SECONDS", 0.03),
        patch.object(charging, "POLL_SECONDS", 0.01),
    ):
        yield


@pytest.fixture
def car(hass: HomeAssistant) -> FakeCar:
    fake = FakeCar(hass)
    fake.set(80)

    async def set_value(call: ServiceCall) -> None:
        value = float(call.data["value"])
        fake.writes.append(value)
        if fake.fail_writes:
            fake.fail_writes -= 1
            raise HomeAssistantError("Command signature/MAC is incorrect")
        if fake.confirm:
            hass.states.async_set(LIMIT, str(value))

    async def press(call: ServiceCall) -> None:
        fake.wakes += 1

    hass.services.async_register("number", "set_value", set_value)
    hass.services.async_register("button", "press", press)
    return fake


@pytest.fixture
async def charger(hass: HomeAssistant, ic, world, car, aioclient_mock):
    aioclient_mock.post(PLAN_URL, status=200)
    aioclient_mock.delete(PLAN_URL, status=200)
    e = make_entry(
        evcc_url=EVCC,
        evcc_vehicle="tesla",
        car_limit_entity=LIMIT,
        car_wake_entity=WAKE,
        car_charging_entity=CHARGING,
    )
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    aioclient_mock.clear_requests()
    aioclient_mock.post(PLAN_URL, status=200)
    aioclient_mock.delete(PLAN_URL, status=200)
    return e.runtime_data.charger


def use_plan(charger, **kw: Any) -> Plan:
    plan = Plan(**kw)
    charger.coordinator.data.plan = plan
    return plan


def trip(soc: float, hours: float = 24, notify: str = "notify.mobile_app_mar"):
    return {
        "soc": soc,
        "kind": "trip",
        "deadline": dt_util.now() + dt.timedelta(hours=hours),
        "uid": "t1",
        "place": "Delfzijl",
        "km": 400.0,
        "notify_service": notify,
    }


def pushes(world, title: str) -> list[str]:
    return [s for s, d in world.notified if d["title"] == title]


def notes(hass: HomeAssistant) -> dict[str, Any]:
    from homeassistant.components import persistent_notification

    return persistent_notification._async_get_or_create_notifications(hass)


def raised(hass: HomeAssistant) -> str:
    return hass.states.get("binary_sensor.ev_trip_planner_limit_raised").state


# ------------------------------------------------------------- raising


async def test_trip_above_normal_raises_and_tells_the_booker(
    hass, charger, car, world
) -> None:
    use_plan(charger, **trip(96.7))
    await charger.async_run("plan")
    await hass.async_block_till_done()
    assert car.wakes == 1
    assert car.writes == [97]
    assert hass.states.get(LIMIT).state == "97.0"
    assert raised(hass) == "on"
    assert pushes(world, "Tesla charge limit raised") == ["mobile_app_mar"]
    # A second run changes nothing and tells nobody again.
    await charger.async_run("hourly")
    assert car.writes == [97]
    assert len(pushes(world, "Tesla charge limit raised")) == 1


async def test_stale_session_is_retried(hass, charger, car, world) -> None:
    car.fail_writes = 1
    use_plan(charger, **trip(95))
    await charger.async_run("plan")
    assert car.writes == [95, 95]
    assert raised(hass) == "on"


async def test_car_never_confirms(hass, charger, car, world) -> None:
    car.confirm = False
    use_plan(charger, **trip(95))
    await charger.async_run("plan")
    assert car.writes == [95, 95, 95]
    assert raised(hass) == "off"
    assert not pushes(world, "Tesla charge limit raised")
    assert notes(hass)["evcc_car_limit"]["title"] == "Tesla charge limit not raised"
    # The car answers on the next run: raised, notice gone.
    car.confirm = True
    await charger.async_run("hourly")
    assert raised(hass) == "on"
    assert "evcc_car_limit" not in notes(hass)


async def test_unknown_limit_is_never_raised_over(hass, charger, car, world) -> None:
    car.set("unavailable")
    use_plan(charger, **trip(95))
    await charger.async_run("plan")
    assert car.writes == []
    assert raised(hass) == "off"
    assert "evcc_car_limit" in notes(hass)


async def test_hand_set_limit_high_enough_stays_a_humans(
    hass, charger, car, world
) -> None:
    # The stale read said 80, the car woke at 100 (set by hand).
    car.set(80)

    async def press(call: ServiceCall) -> None:
        car.wakes += 1
        hass.states.async_set(LIMIT, "100")

    hass.services.async_register("button", "press", press)
    use_plan(charger, **trip(95))
    await charger.async_run("plan")
    assert car.writes == []
    assert raised(hass) == "off"
    assert not pushes(world, "Tesla charge limit raised")


# ------------------------------------------------------------ lowering


async def test_trip_cancelled_after_ha_raised_lowers_silently(
    hass, charger, car, world
) -> None:
    use_plan(charger, **trip(95))
    await charger.async_run("plan")
    use_plan(charger, soc=50, kind="floor", deadline=dt_util.now())
    await charger.async_run("plan")
    assert car.writes == [95, 80]
    assert raised(hass) == "off"
    assert not pushes(world, "Tesla charge limit reset")


async def test_hand_set_with_only_a_floor_plan_is_held(
    hass, charger, car, world
) -> None:
    car.set(100)
    use_plan(charger, soc=50, kind="floor", deadline=dt_util.now())
    await charger.async_run("hourly")
    assert car.writes == []


async def test_hand_set_and_a_small_booked_trip_lowers_with_a_push(
    hass, charger, car, world
) -> None:
    car.set(100)
    use_plan(charger, **trip(60))
    await charger.async_run("plan")
    assert car.writes == [80]
    assert pushes(world, "Tesla charge limit reset") == ["mobile_app_mar"]


async def test_never_lowered_while_charging(hass, charger, car, world) -> None:
    car.set(100, soc=87, charging_state="charging")
    use_plan(charger, **trip(60))
    await charger.async_run("plan")
    assert car.writes == []


async def test_hand_set_charge_reached_lowers(hass, charger, car, world) -> None:
    car.set(100, soc=100, charging_state="complete")
    use_plan(charger, soc=50, kind="floor", deadline=dt_util.now())
    await charger.async_run("charge_ended")
    assert car.writes == [80]
    assert pushes(world, "Tesla charge limit reset")


async def test_hand_set_charge_interrupted_is_held(hass, charger, car, world) -> None:
    car.set(100, soc=70, charging_state="stopped")
    use_plan(charger, soc=50, kind="floor", deadline=dt_util.now())
    await charger.async_run("charge_ended")
    assert car.writes == []


async def test_charge_ended_triggers_a_run(hass, charger, car, world) -> None:
    car.set(100, soc=100, charging_state="charging")
    use_plan(charger, soc=50, kind="floor", deadline=dt_util.now())
    hass.states.async_set(CHARGING, "complete")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert car.writes == [80]


async def test_legacy_helper_carried_over(
    hass: HomeAssistant, ic, world, car, aioclient_mock
) -> None:
    hass.states.async_set("input_boolean.evcc_car_limit_raised", "on")
    aioclient_mock.post(PLAN_URL, status=200)
    aioclient_mock.delete(PLAN_URL, status=200)
    e = make_entry(car_limit_entity=LIMIT)
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert raised(hass) == "on"


# ---------------------------------------------------------------- evcc


async def test_publish_trip_plan(hass, charger, car, aioclient_mock) -> None:
    plan = use_plan(charger, **trip(42.1, hours=10))
    await charger.async_run("plan")
    ((method, url, *_),) = aioclient_mock.mock_calls
    assert method == "POST"
    stamp = dt_util.as_utc(plan.deadline).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert str(url) == f"{EVCC}/api/vehicles/tesla/plan/soc/43/{stamp}"


async def test_late_plan_sent_fifteen_minutes_ahead(
    hass, charger, car, aioclient_mock
) -> None:
    use_plan(charger, **trip(42, hours=-1))
    before = dt_util.utcnow()
    await charger.async_run("plan")
    ((_, url, *_),) = aioclient_mock.mock_calls
    sent = dt.datetime.strptime(str(url).rsplit("/", 1)[1], "%Y-%m-%dT%H:%M:%SZ")
    sent = sent.replace(tzinfo=dt.UTC)
    assert dt.timedelta(minutes=14) <= sent - before <= dt.timedelta(minutes=16)


async def test_idle_deletes_the_plan(hass, charger, car, aioclient_mock) -> None:
    use_plan(charger, soc=0, kind="idle")
    await charger.async_run("hourly")
    ((method, url, *_),) = aioclient_mock.mock_calls
    assert method == "DELETE"
    assert str(url) == f"{EVCC}/api/vehicles/tesla/plan/soc"


async def test_evcc_failure_noted_then_cleared(
    hass, charger, car, aioclient_mock
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(PLAN_URL, status=500)
    use_plan(charger, **trip(42))
    await charger.async_run("plan")
    assert "(500)" in notes(hass)["evcc_plan_publish"]["message"]
    aioclient_mock.clear_requests()
    aioclient_mock.post(PLAN_URL, status=200)
    await charger.async_run("hourly")
    assert "evcc_plan_publish" not in notes(hass)


async def test_evcc_unreachable(hass, charger, car, aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(PLAN_URL, exc=TimeoutError())
    use_plan(charger, **trip(42))
    await charger.async_run("plan")
    assert "no response" in notes(hass)["evcc_plan_publish"]["message"]


async def test_a_new_plan_publishes(hass, charger, car, aioclient_mock) -> None:
    """End to end: booking a trip reaches evcc without a manual run."""
    from .test_services import at, call, wall

    await hass.async_block_till_done(wait_background_tasks=True)
    aioclient_mock.clear_requests()
    aioclient_mock.post(PLAN_URL, status=200)
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=1, hour=9)),
        place="Delft",
        location="Delft",
        geo="52.01,4.36",
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert any(m == "POST" for m, *_ in aioclient_mock.mock_calls)


async def test_not_configured_does_nothing(hass, entry, car, aioclient_mock) -> None:
    charger = entry.runtime_data.charger
    assert not charger.enabled
    use_plan(charger, **trip(95))
    await charger.async_run("plan")
    assert car.writes == []
    assert not aioclient_mock.mock_calls
