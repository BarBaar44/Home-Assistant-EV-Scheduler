"""EV Trip Planner: plan EV charging around the trips on an Invite
Calendar, and serve the EV Trip Card (contract v1)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_CALENDAR,
    DOMAIN,
    IC_DOMAIN,
    SERVICE_CANCEL,
    SERVICE_CLEAR_SEARCH,
    SERVICE_CLEAR_STATUS,
    SERVICE_MOVE,
    SERVICE_REFRESH,
    SERVICE_SCHEDULE,
    SERVICE_SEARCH,
    SERVICE_SET_STATUS,
    STATUS_LEVELS,
)
from .coordinator import TripPlannerCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type TripPlannerConfigEntry = ConfigEntry[TripPlannerCoordinator]

_ENTRY = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> TripPlannerCoordinator:
    """The entry a service call is for: the given one, or the only one."""
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if wanted:
        entries = [e for e in entries if e.entry_id == wanted]
    if len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_entry" if not entries else "which_entry",
        )
    return entries[0].runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the contract v1 services."""

    async def search(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_search(call.data["query"])

    async def clear_search(call: ServiceCall) -> None:
        _coordinator(hass, call).clear_search()

    async def schedule(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_schedule(
            start=call.data["start"],
            place=call.data["place"],
            location=call.data["location"],
            geo=call.data.get("geo"),
            user_id=call.data.get("user_id") or call.context.user_id,
            one_way=call.data["one_way"],
            arrive_by=call.data["arrive_by"],
        )

    async def move(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_move(call.data["uid"], call.data["start"])

    async def cancel(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_cancel(call.data["uid"])

    async def set_status(call: ServiceCall) -> None:
        _coordinator(hass, call).set_card_status(
            call.data["level"], call.data["message"]
        )

    async def clear_status(call: ServiceCall) -> None:
        _coordinator(hass, call).clear_status(force=True)

    async def refresh(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_refresh()

    services: dict[str, tuple[Any, dict[Any, Any]]] = {
        SERVICE_SEARCH: (search, {vol.Required("query"): cv.string}),
        SERVICE_CLEAR_SEARCH: (clear_search, {}),
        SERVICE_SCHEDULE: (
            schedule,
            {
                vol.Required("start"): cv.datetime,
                vol.Required("place"): cv.string,
                vol.Required("location"): cv.string,
                vol.Optional("geo"): vol.Any(cv.string, None),
                vol.Optional("user_id"): cv.string,
                vol.Optional("one_way", default=False): cv.boolean,
                vol.Optional("arrive_by", default=False): cv.boolean,
            },
        ),
        SERVICE_MOVE: (
            move,
            {vol.Required("uid"): cv.string, vol.Required("start"): cv.datetime},
        ),
        SERVICE_CANCEL: (cancel, {vol.Required("uid"): cv.string}),
        SERVICE_SET_STATUS: (
            set_status,
            {
                vol.Required("level"): vol.In(STATUS_LEVELS),
                vol.Required("message"): cv.string,
            },
        ),
        SERVICE_CLEAR_STATUS: (clear_status, {}),
        SERVICE_REFRESH: (refresh, {}),
    }
    for name, (handler, schema) in services.items():
        hass.services.async_register(
            DOMAIN, name, handler, schema=vol.Schema({**_ENTRY, **schema})
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TripPlannerConfigEntry) -> bool:
    """Set up a planner for one trip calendar."""
    calendar = entry.data[CONF_CALENDAR]
    if IC_DOMAIN not in hass.config.components or hass.states.get(calendar) is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="calendar_missing",
            translation_placeholders={"calendar": calendar},
        )
    coordinator = TripPlannerCoordinator(hass, entry)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_subscribe()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options or household members changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: TripPlannerConfigEntry
) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
