"""The four contract v1 sensors. Their entity ids are fixed
(sensor.ev_trip_planner_<key>) so the card config never depends on the
language Home Assistant runs in."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TripPlannerConfigEntry
from .const import CONTRACT_VERSION, DOMAIN
from .coordinator import TripPlannerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TripPlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            TripsSensor(coordinator),
            SearchSensor(coordinator),
            StatusSensor(coordinator),
            PlanSensor(coordinator),
        ]
    )


class _Base(CoordinatorEntity[TripPlannerCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _key: str

    def __init__(self, coordinator: TripPlannerCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_translation_key = self._key
        self.entity_id = f"sensor.{DOMAIN}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EV trip planner",
            entry_type=DeviceEntryType.SERVICE,
        )


class TripsSensor(_Base):
    """State: number of upcoming trips; `trips`: the list, soonest first."""

    _key = "trips"
    _attr_icon = "mdi:car-clock"
    _unrecorded_attributes = frozenset({"trips"})

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.trips)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"version": CONTRACT_VERSION, "trips": self.coordinator.data.trips}


class SearchSensor(_Base):
    """State: idle | results | empty | failed."""

    _key = "search"
    _attr_icon = "mdi:map-search-outline"
    _unrecorded_attributes = frozenset({"results", "query"})

    @property
    def native_value(self) -> str:
        return self.coordinator.search.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        search = self.coordinator.search
        return {
            "version": CONTRACT_VERSION,
            "query": search.query,
            "results": search.results,
        }


class StatusSensor(_Base):
    """The card's banner. State: ok | info | success | warning | error."""

    _key = "status"
    _attr_icon = "mdi:message-alert-outline"
    _unrecorded_attributes = frozenset({"message"})

    @property
    def native_value(self) -> str:
        return self.coordinator.status.level

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.coordinator.status
        return {
            "version": CONTRACT_VERSION,
            "level": status.level,
            "message": status.message,
            "source": status.source,
            "updated": status.updated.isoformat(timespec="seconds"),
        }


class PlanSensor(_Base):
    """State: the SOC published for the charger, 0 when idle."""

    _key = "plan"
    _attr_icon = "mdi:ev-station"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float:
        return self.coordinator.data.plan.soc

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.plan_dict()
        plan.pop("soc", None)
        return plan
