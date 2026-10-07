"""Config flow, options and household member subentries."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ev_trip_planner.const import (
    CONF_BATTERY_KWH,
    CONF_CALENDAR,
    CONF_CONTACT,
    CONF_DEFAULT_NOTIFY,
    CONF_EMAIL,
    CONF_NOTIFY,
    CONF_USER_ID,
    DOMAIN,
    OPT_FLOOR_SOC,
    SUBENTRY_MEMBER,
)

from .conftest import CAL


def _user_input(**over):
    return {
        CONF_CALENDAR: CAL,
        CONF_CONTACT: "me@example.com",
        CONF_BATTERY_KWH: 79.0,
        CONF_DEFAULT_NOTIFY: "notify.mobile_app_bart",
        **over,
    }


async def test_aborts_without_invite_calendar(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invite_calendar_missing"


async def test_user_flow(hass: HomeAssistant, ic, world) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(**{CONF_DEFAULT_NOTIFY: "notify.notify"})
    )
    assert result["errors"] == {CONF_DEFAULT_NOTIFY: "notify_broadcast"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _user_input(**{CONF_DEFAULT_NOTIFY: "notify.nope", CONF_CONTACT: "x"}),
    )
    assert result["errors"] == {
        CONF_DEFAULT_NOTIFY: "notify_unknown",
        CONF_CONTACT: "contact_invalid",
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input()
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "tesla"
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ev_trip_planner_plan") is not None

    # One planner per calendar.
    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    again = await hass.config_entries.flow.async_configure(
        again["flow_id"], _user_input()
    )
    assert again["reason"] == "already_configured"


async def test_options(hass: HomeAssistant, entry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    data = {
        "safety_buffer_pct": 10,
        "fallback_efficiency_wh_km": 153,
        "lookahead_days": 7,
        "prep_buffer_min": 15,
        "trip_cluster_hours": 12,
        "all_day_departure_hour": 8,
        OPT_FLOOR_SOC: 50,
        "floor_ready_hour": 7,
        "trip_duration_min": 60,
        "accept_invites": True,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    # Reloaded with the floor.
    plan = hass.states.get("sensor.ev_trip_planner_plan")
    assert plan.attributes["kind"] == "floor"


async def test_member_subentry(hass: HomeAssistant, entry, hass_admin_user) -> None:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_MEMBER),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    member = {
        CONF_USER_ID: hass_admin_user.id,
        CONF_EMAIL: "admin@example.com",
        CONF_NOTIFY: "notify.mobile_app_mar",
    }
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**member, CONF_NOTIFY: "notify.notify"}
    )
    assert result["errors"] == {CONF_NOTIFY: "notify_broadcast"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], member
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    members = entry.runtime_data.settings.members
    assert hass_admin_user.id in {m.user_id for m in members}

    # The same user twice.
    again = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_MEMBER),
        context={"source": config_entries.SOURCE_USER},
    )
    again = await hass.config_entries.subentries.async_configure(
        again["flow_id"], member
    )
    assert again["errors"] == {CONF_USER_ID: "member_exists"}
