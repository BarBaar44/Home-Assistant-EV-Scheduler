"""binary_sensor.ev_trip_planner_limit_raised: the planner raised the car's
charge limit above normal for a trip, and the car confirmed it. Replaces
the automation's input_boolean.evcc_car_limit_raised; the card shows it as
"raised" next to the limit."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TripPlannerConfigEntry
from .const import DOMAIN
from .coordinator import TripPlannerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TripPlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([LimitRaisedSensor(entry.runtime_data)])


class LimitRaisedSensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "limit_raised"
    _attr_icon = "mdi:battery-arrow-up"

    def __init__(self, coordinator: TripPlannerCoordinator) -> None:
        entry = coordinator.config_entry
        self._charger = coordinator.charger
        self._attr_unique_id = f"{entry.entry_id}_limit_raised"
        self.entity_id = f"binary_sensor.{DOMAIN}_limit_raised"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EV Trip Planner",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        return self._charger.limit_raised

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._charger.async_add_listener(self.async_write_ha_state)
        )
