"""Entry settings in one typed object, with the defaults in one place."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .const import (
    BROADCAST_NOTIFY,
    CONF_BATTERY_KWH,
    CONF_CALENDAR,
    CONF_CONTACT,
    CONF_DEFAULT_NOTIFY,
    CONF_EFFICIENCY_SENSOR,
    CONF_EMAIL,
    CONF_NOTIFY,
    CONF_SOC_SENSOR,
    CONF_USER_ID,
    OPT_ACCEPT_INVITES,
    OPT_ALL_DAY_HOUR,
    OPT_CLUSTER_HOURS,
    OPT_FALLBACK_WH_KM,
    OPT_FLOOR_READY_HOUR,
    OPT_FLOOR_SOC,
    OPT_LOOKAHEAD_DAYS,
    OPT_PREP_BUFFER,
    OPT_SAFETY_BUFFER,
    OPT_TRIP_DURATION,
    SUBENTRY_MEMBER,
)

DEFAULTS = {
    OPT_SAFETY_BUFFER: 10.0,
    OPT_FALLBACK_WH_KM: 153.0,
    OPT_LOOKAHEAD_DAYS: 7,
    OPT_PREP_BUFFER: 15,
    OPT_CLUSTER_HOURS: 12.0,
    OPT_ALL_DAY_HOUR: 8,
    OPT_FLOOR_SOC: 0.0,
    OPT_FLOOR_READY_HOUR: 7,
    OPT_TRIP_DURATION: 60,
    OPT_ACCEPT_INVITES: True,
}

SAFE_NOTIFY = "notify.persistent_notification"


@dataclass(frozen=True)
class Member:
    """A household member who can book trips and gets their alerts."""

    user_id: str
    email: str
    notify_service: str


@dataclass(frozen=True)
class Settings:
    calendar: str
    contact: str
    battery_kwh: float
    soc_sensor: str | None
    efficiency_sensor: str | None
    default_notify: str
    safety_buffer: float
    fallback_wh_km: float
    lookahead_days: int
    prep_buffer: float
    cluster_hours: float
    all_day_hour: int
    floor_soc: float
    floor_ready_hour: int
    trip_duration: int
    accept_invites: bool
    members: tuple[Member, ...]

    def member_by_user(self, user_id: str | None) -> Member | None:
        return next((m for m in self.members if m.user_id == user_id), None)

    def notify_for(self, email: str | None) -> tuple[str, str | None]:
        """(notify service, reason when it is the fallback). Case blind:
        calendars rewrite address case freely."""
        if not email:
            return self.default_notify, "no person on file for this event"
        wanted = email.strip().lower()
        for m in self.members:
            if m.email.strip().lower() == wanted:
                if m.notify_service:
                    return m.notify_service, None
                return self.default_notify, f"{email} has no notify service"
        return self.default_notify, f"{email} is not a household member"


def safe_notify(service: str | None) -> str:
    """Never a broadcast: notify.notify would push one person's alert to
    every phone in the house."""
    service = (service or "").strip()
    if service in BROADCAST_NOTIFY or not service.startswith("notify."):
        return SAFE_NOTIFY
    return service


def settings(entry: ConfigEntry) -> Settings:
    data = entry.data
    opts = {**DEFAULTS, **entry.options}
    members = tuple(
        Member(
            user_id=sub.data[CONF_USER_ID],
            email=sub.data[CONF_EMAIL],
            notify_service=safe_notify(sub.data.get(CONF_NOTIFY)),
        )
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_MEMBER
    )
    return Settings(
        calendar=data[CONF_CALENDAR],
        contact=data[CONF_CONTACT],
        battery_kwh=float(data[CONF_BATTERY_KWH]),
        soc_sensor=data.get(CONF_SOC_SENSOR) or None,
        efficiency_sensor=data.get(CONF_EFFICIENCY_SENSOR) or None,
        default_notify=safe_notify(data.get(CONF_DEFAULT_NOTIFY)),
        safety_buffer=float(opts[OPT_SAFETY_BUFFER]),
        fallback_wh_km=float(opts[OPT_FALLBACK_WH_KM]),
        lookahead_days=int(opts[OPT_LOOKAHEAD_DAYS]),
        prep_buffer=float(opts[OPT_PREP_BUFFER]),
        cluster_hours=float(opts[OPT_CLUSTER_HOURS]),
        all_day_hour=int(opts[OPT_ALL_DAY_HOUR]),
        floor_soc=float(opts[OPT_FLOOR_SOC]),
        floor_ready_hour=int(opts[OPT_FLOOR_READY_HOUR]),
        trip_duration=int(opts[OPT_TRIP_DURATION]),
        accept_invites=bool(opts[OPT_ACCEPT_INVITES]),
        members=members,
    )
