"""Fixtures: a fake Invite Calendar and a set up planner.

The fake follows what Invite Calendar 1.2.2 actually returns (read from
its calendar.py / coordinator.py and seen live through the pyscript apps),
not what this integration expects: list_events entries carry
recurrence_id None for single events, status None when unset, ISO starts
with an offset; create/update answer uid, invited, pending; cancel sends
the CANCEL first and fails without changing anything when mail is down."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_trip_planner.const import (
    CONF_BATTERY_KWH,
    CONF_CALENDAR,
    CONF_CONTACT,
    CONF_DEFAULT_NOTIFY,
    CONF_EMAIL,
    CONF_NOTIFY,
    CONF_SOC_SENSOR,
    CONF_USER_ID,
    DOMAIN,
    SUBENTRY_MEMBER,
)

CAL = "calendar.tesla"
OWN = "tesla@example.com"
HOME = (52.09, 5.12)  # Utrecht
HAARLEM = (52.38, 4.64)
DELFT = (52.01, 4.36)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    return


@pytest.fixture(autouse=True)
async def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    hass.states.async_set("zone.home", "0", {"latitude": HOME[0], "longitude": HOME[1]})
    return tmp_path


@dataclass
class FakeInviteCalendar:
    hass: HomeAssistant
    events: list[dict[str, Any]] = field(default_factory=list)
    smtp_down: bool = False
    fail_list: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fired: list[dict[str, Any]] = field(default_factory=list)

    def add(self, **kw: Any) -> dict[str, Any]:
        ev = {
            "uid": f"{uuid.uuid4()}@example.com",
            "recurrence_id": None,
            "all_day": False,
            "summary": "",
            "description": "",
            "location": "",
            "organizer": OWN,
            "attendees": [],
            "status": None,
            "sequence": 0,
            "managed": True,
            "accepted": False,
        }
        ev.update(kw)
        self.events.append(ev)
        return ev

    def _find(self, uid: str) -> dict[str, Any]:
        for ev in self.events:
            if ev["uid"] == uid:
                return ev
        raise HomeAssistantError(f"No event with UID {uid} in this calendar.")

    def _fire(self, added=(), updated=(), removed=()) -> None:
        data = {
            "entity_id": CAL,
            "added": list(added),
            "updated": list(updated),
            "removed": list(removed),
        }
        self.fired.append(data)
        self.hass.bus.async_fire("invite_calendar_updated", data)

    @staticmethod
    def _aware(value: Any) -> dt.datetime:
        if isinstance(value, str):
            value = dt.datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_util.get_default_time_zone())
        return value

    def handle(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, data))
        return getattr(self, action)(**data)

    def list_events(self, start=None, end=None, duration=None) -> dict[str, Any]:
        if self.fail_list:
            raise HomeAssistantError("Could not read the calendar: boom")
        s = self._aware(start) if start else dt_util.now()
        e = s + (duration or dt.timedelta(days=7))
        out = []
        for ev in sorted(self.events, key=lambda x: x["start"]):
            if ev["end"] > s and ev["start"] < e:
                d = dict(ev)
                if ev["all_day"]:
                    d["start"] = ev["start"].date().isoformat()
                    d["end"] = ev["end"].date().isoformat()
                else:
                    d["start"] = ev["start"].isoformat()
                    d["end"] = ev["end"].isoformat()
                d["own"] = ev["organizer"].lower() == OWN
                out.append(d)
        return {"events": out}

    def create_event(
        self,
        summary,
        start_date_time,
        end_date_time=None,
        description=None,
        location=None,
        attendees=None,
    ) -> dict[str, Any]:
        s = self._aware(start_date_time)
        e = self._aware(end_date_time) if end_date_time else s + dt.timedelta(hours=1)
        ev = self.add(
            summary=summary,
            start=s,
            end=e,
            description=description or "",
            location=location or "",
            attendees=list(attendees or []),
        )
        self._fire(added=[ev["uid"]])
        return {"uid": ev["uid"], "invited": ev["attendees"], "pending": self.smtp_down}

    def update_event(self, uid, start_date_time=None) -> dict[str, Any]:
        ev = self._find(uid)
        if ev["organizer"] != OWN:
            raise HomeAssistantError(f"Event {uid} is organized by someone else.")
        if start_date_time:
            length = ev["end"] - ev["start"]
            ev["start"] = self._aware(start_date_time)
            ev["end"] = ev["start"] + length
        ev["sequence"] += 1
        self._fire(updated=[uid])
        return {
            "uid": uid,
            "sequence": ev["sequence"],
            "invited": ev["attendees"],
            "removed": [],
            "pending": self.smtp_down,
        }

    def cancel_event(self, uid) -> dict[str, Any]:
        ev = self._find(uid)
        if ev["organizer"] != OWN:
            raise HomeAssistantError(f"Event {uid} is organized by someone else.")
        if ev["attendees"] and self.smtp_down:
            raise HomeAssistantError("Sending the cancellation failed: SMTP down")
        self.events.remove(ev)
        self._fire(removed=[uid])
        return {"uid": uid, "notified": ev["attendees"]}

    def accept_event(self, uid) -> dict[str, Any]:
        ev = self._find(uid)
        if not ev["managed"]:
            raise HomeAssistantError(f"Event {uid} did not arrive by mail.")
        if ev["accepted"]:
            return {"uid": uid, "sequence": ev["sequence"], "sent": False}
        ev["accepted"] = True
        return {"uid": uid, "sequence": ev["sequence"], "sent": True}


@pytest.fixture
def ic(hass: HomeAssistant) -> FakeInviteCalendar:
    fake = FakeInviteCalendar(hass)
    hass.config.components.add("invite_calendar")
    hass.states.async_set(CAL, "off", {"friendly_name": "tesla"})

    for action in (
        "list_events",
        "create_event",
        "update_event",
        "cancel_event",
        "accept_event",
    ):

        def make(action: str):
            async def handler(call: ServiceCall) -> dict[str, Any]:
                data = dict(call.data)
                assert data.pop("entity_id") in (CAL, [CAL])
                return {CAL: fake.handle(action, data)}

            return handler

        hass.services.async_register(
            "invite_calendar",
            action,
            make(action),
            supports_response=SupportsResponse.OPTIONAL,
        )
    return fake


@dataclass
class FakeWorld:
    """Nominatim and Waze stand ins."""

    places: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    nominatim_down: bool = False
    waze_min: float | None = 60.0
    waze_km: float = 50.0
    waze_calls: list[tuple] = field(default_factory=list)
    notified: list[tuple[str, dict]] = field(default_factory=list)


def nominatim(lat, lon, display, road=None, number=None, city=None):
    return {
        "lat": str(lat),
        "lon": str(lon),
        "display_name": display,
        "address": {"road": road, "house_number": number, "city": city},
    }


@pytest.fixture
def world(hass: HomeAssistant) -> Generator[FakeWorld]:
    w = FakeWorld(
        places={
            "lidl delft": [
                nominatim(
                    52.0012,
                    4.3712,
                    "Lidl, 119, Papsouwselaan, Delft",
                    "Papsouwselaan",
                    "119",
                    "Delft",
                ),
                nominatim(
                    52.0112,
                    4.3512,
                    "Lidl, Westlandseweg, Delft",
                    "Westlandseweg",
                    None,
                    "Delft",
                ),
            ],
            "markt 14 delft": [
                nominatim(52.011, 4.358, "12, Markt, Delft", "Markt", "12", "Delft")
            ],
            "delft": [nominatim(*DELFT, "Delft, Nederland", city="Delft")],
            "haarlem": [nominatim(*HAARLEM, "Haarlem, Nederland", city="Haarlem")],
            "paris": [nominatim(48.85, 2.35, "Paris, France", city="Paris")],
        }
    )

    async def fake_get(self, params):
        from custom_components.ev_trip_planner.geocode import LookupFailed

        if w.nominatim_down:
            raise LookupFailed("down")
        return w.places.get(params["q"].strip().lower(), [])

    def fake_waze(home, dest, out_at, back_at):
        w.waze_calls.append((home, dest, out_at, back_at))
        if w.waze_min is None:
            raise RuntimeError("HTTP 403")
        back = (w.waze_km, w.waze_min) if back_at is not None else None
        return (w.waze_km, w.waze_min), back

    async def notify(call: ServiceCall) -> None:
        w.notified.append((call.service, dict(call.data)))

    for name in ("mobile_app_bart", "mobile_app_mar", "persistent_notification"):
        hass.services.async_register("notify", name, notify)

    with (
        patch("custom_components.ev_trip_planner.geocode.Geocoder._get", fake_get),
        patch("custom_components.ev_trip_planner.routing.waze_legs", fake_waze),
    ):
        yield w


def make_entry(**options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="tesla",
        unique_id=CAL,
        data={
            CONF_CALENDAR: CAL,
            CONF_CONTACT: "me@example.com",
            CONF_BATTERY_KWH: 79.0,
            CONF_SOC_SENSOR: "sensor.car_battery",
            CONF_DEFAULT_NOTIFY: "notify.mobile_app_bart",
        },
        options=options,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_MEMBER,
                title="Bart",
                unique_id=None,
                data={
                    CONF_USER_ID: "u_bart",
                    CONF_EMAIL: "bart@example.com",
                    CONF_NOTIFY: "notify.mobile_app_bart",
                },
            ),
            ConfigSubentryData(
                subentry_type=SUBENTRY_MEMBER,
                title="Marjolijn",
                unique_id=None,
                data={
                    CONF_USER_ID: "u_mar",
                    CONF_EMAIL: "Marjolijn@example.com",
                    CONF_NOTIFY: "notify.mobile_app_mar",
                },
            ),
        ],
    )


@pytest.fixture
async def entry(hass: HomeAssistant, ic, world) -> MockConfigEntry:
    e = make_entry()
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


def at(days: float = 0, hour: int | None = None, minutes: float = 0) -> dt.datetime:
    base = dt_util.now().replace(second=0, microsecond=0)
    if hour is not None:
        base = base.replace(hour=hour, minute=0)
    return base + dt.timedelta(days=days, minutes=minutes)


def wall(when: dt.datetime) -> str:
    """Naive local wall time, as the card sends it."""
    return when.strftime("%Y-%m-%dT%H:%M:%S")
