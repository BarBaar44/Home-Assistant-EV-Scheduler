"""Config flow: the trip calendar and the car; options for the planning
knobs; household members as subentries."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

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
    DOMAIN,
    IC_DOMAIN,
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
from .options import DEFAULTS


def _notify_selector(hass: HomeAssistant) -> SelectSelector:
    """Notify services one person receives; never the broadcast."""
    services = sorted(
        f"notify.{name}"
        for name in hass.services.async_services_for_domain("notify")
        if f"notify.{name}" not in BROADCAST_NOTIFY
    )
    return SelectSelector(
        SelectSelectorConfig(
            options=services, custom_value=True, mode=SelectSelectorMode.DROPDOWN
        )
    )


def _notify_error(hass: HomeAssistant, service: str) -> str | None:
    service = (service or "").strip()
    if service in BROADCAST_NOTIFY:
        return "notify_broadcast"
    domain, _, name = service.partition(".")
    if domain != "notify" or not hass.services.has_service(domain, name):
        return "notify_unknown"
    return None


def _number(low: float, high: float, step: float, unit: str | None = None):
    return NumberSelector(
        NumberSelectorConfig(
            min=low,
            max=high,
            step=step,
            unit_of_measurement=unit,
            mode=NumberSelectorMode.BOX,
        )
    )


class TripPlannerConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per trip calendar."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if IC_DOMAIN not in self.hass.config.components:
            return self.async_abort(reason="invite_calendar_missing")
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_CALENDAR])
            self._abort_if_unique_id_configured()
            if err := _notify_error(self.hass, user_input[CONF_DEFAULT_NOTIFY]):
                errors[CONF_DEFAULT_NOTIFY] = err
            if "@" not in user_input[CONF_CONTACT]:
                errors[CONF_CONTACT] = "contact_invalid"
            if not errors:
                state = self.hass.states.get(user_input[CONF_CALENDAR])
                name = state.name if state else user_input[CONF_CALENDAR]
                return self.async_create_entry(title=name, data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_CALENDAR): EntitySelector(
                    EntitySelectorConfig(domain="calendar", integration=IC_DOMAIN)
                ),
                vol.Required(CONF_CONTACT): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL)
                ),
                vol.Required(CONF_BATTERY_KWH, default=79.0): _number(
                    10, 200, 0.1, "kWh"
                ),
                vol.Optional(CONF_SOC_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="battery")
                ),
                vol.Optional(CONF_EFFICIENCY_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_DEFAULT_NOTIFY): _notify_selector(self.hass),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> TripPlannerOptionsFlow:
        return TripPlannerOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_MEMBER: MemberSubentryFlow}


class TripPlannerOptionsFlow(OptionsFlow):
    """The planning knobs."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**DEFAULTS, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(OPT_SAFETY_BUFFER): _number(0, 50, 1, "%"),
                vol.Required(OPT_FALLBACK_WH_KM): _number(80, 400, 1, "Wh/km"),
                vol.Required(OPT_LOOKAHEAD_DAYS): _number(1, 30, 1, "d"),
                vol.Required(OPT_PREP_BUFFER): _number(0, 120, 1, "min"),
                vol.Required(OPT_CLUSTER_HOURS): _number(0, 48, 1, "h"),
                vol.Required(OPT_ALL_DAY_HOUR): _number(0, 23, 1, "h"),
                vol.Required(OPT_FLOOR_SOC): _number(0, 80, 1, "%"),
                vol.Required(OPT_FLOOR_READY_HOUR): _number(0, 23, 1, "h"),
                vol.Required(OPT_TRIP_DURATION): _number(15, 1440, 15, "min"),
                vol.Required(OPT_ACCEPT_INVITES): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
        )


class MemberSubentryFlow(ConfigSubentryFlow):
    """A household member: who books trips, where invites and alerts go."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._form("user", user_input, None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        return await self._form("reconfigure", user_input, dict(sub.data))

    async def _form(
        self,
        step: str,
        user_input: dict[str, Any] | None,
        current: dict[str, Any] | None,
    ) -> SubentryFlowResult:
        users = [
            u
            for u in await self.hass.auth.async_get_users()
            if u.is_active and not u.system_generated
        ]
        names = {u.id: u.name or u.id for u in users}
        errors: dict[str, str] = {}
        entry = self._get_entry()
        if user_input is not None:
            taken = {
                s.data[CONF_USER_ID]
                for s in entry.subentries.values()
                if s.subentry_type == SUBENTRY_MEMBER
                and (current is None or s.data[CONF_USER_ID] != current[CONF_USER_ID])
            }
            if user_input[CONF_USER_ID] in taken:
                errors[CONF_USER_ID] = "member_exists"
            if "@" not in user_input[CONF_EMAIL]:
                errors[CONF_EMAIL] = "contact_invalid"
            if err := _notify_error(self.hass, user_input[CONF_NOTIFY]):
                errors[CONF_NOTIFY] = err
            if not errors:
                title = names.get(user_input[CONF_USER_ID], user_input[CONF_EMAIL])
                if step == "reconfigure":
                    return self.async_update_and_abort(
                        entry,
                        self._get_reconfigure_subentry(),
                        data=user_input,
                        title=title,
                    )
                return self.async_create_entry(title=title, data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_USER_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=uid, label=name)
                            for uid, name in sorted(names.items(), key=lambda i: i[1])
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_EMAIL): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL)
                ),
                vol.Required(CONF_NOTIFY): _notify_selector(self.hass),
            }
        )
        return self.async_show_form(
            step_id=step,
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or current or {}
            ),
            errors=errors,
        )
