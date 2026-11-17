"""Daily activity stats sensors for OnlyCat: per chip and per device."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import OnlyCatStatsCoordinator
    from .data.activity_stats import StatsWindow
    from .data.device import Device
    from .data.pet import Pet


@dataclass(frozen=True, kw_only=True)
class OnlyCatActivitySensorDescription(SensorEntityDescription):
    """A daily count, keyed by its field in the gateway's stats rows."""

    stat: str


def _count(key: str, stat: str, icon: str) -> OnlyCatActivitySensorDescription:
    return OnlyCatActivitySensorDescription(
        key=key,
        stat=stat,
        icon=icon,
        translation_key=key,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )


CHIP_SENSORS = (
    _count("transits_in_today", "transitsIn", "mdi:home-import-outline"),
    _count("transits_out_today", "transitsOut", "mdi:home-export-outline"),
    _count("peeks_today", "peeks", "mdi:eye-outline"),
    _count("denies_today", "denies", "mdi:cancel"),
    _count("prey_attempts_today", "preyAttempts", "mdi:rodent"),
)

DEVICE_SENSORS = (
    *CHIP_SENSORS,
    _count("events_today", "totalEvents", "mdi:counter"),
    _count("human_activity_today", "humanActivityEvents", "mdi:human"),
    _count("remote_unlocks_today", "remoteUnlocks", "mdi:cellphone-key"),
    _count("manual_unlocks_today", "manualUnlocks", "mdi:lock-open-variant-outline"),
    _count("unidentified_transits_today", "unidentifiedTransits", "mdi:help-circle"),
)


class OnlyCatActivitySensor(CoordinatorEntity["OnlyCatStatsCoordinator"], SensorEntity):
    """One of today's activity counts, with its seven-day total."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"hour_histogram_7d"})
    entity_description: OnlyCatActivitySensorDescription

    def __init__(
        self,
        coordinator: OnlyCatStatsCoordinator,
        description: OnlyCatActivitySensorDescription,
        device: Device,
    ) -> None:
        """Initialize the sensor on its flap device."""
        super().__init__(coordinator)
        self.entity_description = description
        self.device = device

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to map to a device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.device_id)},
            name=self.device.description,
            serial_number=self.device.device_id,
        )

    def _window(self) -> StatsWindow | None:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        """Return whether the last stats fetch worked."""
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> int | None:
        """Return today's count."""
        window = self._window()
        return (
            None if window is None else window.get_today(self.entity_description.stat)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the seven-day total, the summed hour histogram and the day."""
        window = self._window()
        if window is None:
            return None
        return {
            "last_7_days": window.get_last_7_days(self.entity_description.stat),
            "hour_histogram_7d": list(window.hour_histogram_7d),
            "day": self.coordinator.data.day.isoformat()
            if self.coordinator.data.day
            else None,
        }


class OnlyCatChipActivitySensor(OnlyCatActivitySensor):
    """A chip's daily count, summed over the account's flaps."""

    def __init__(
        self,
        coordinator: OnlyCatStatsCoordinator,
        description: OnlyCatActivitySensorDescription,
        device: Device,
        pet: Pet,
    ) -> None:
        """Initialize the sensor for one chip."""
        super().__init__(coordinator, description, device)
        self.pet = pet
        pet_name = pet.label if pet.label is not None else pet.rfid_code
        self._attr_translation_key = f"pet_{description.key}"
        self._attr_translation_placeholders = {"pet_name": pet_name}
        self._attr_unique_id = f"{pet.rfid_code}_{description.key}"
        self.entity_id = f"sensor.{self._attr_unique_id}"
        # Chips nobody named are usually visiting cats: there, but off by default.
        self._attr_entity_registry_enabled_default = pet.label is not None

    def _window(self) -> StatsWindow | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.chip(self.pet.rfid_code)


class OnlyCatDeviceActivitySensor(OnlyCatActivitySensor):
    """A flap's daily count for every cat together."""

    def __init__(
        self,
        coordinator: OnlyCatStatsCoordinator,
        description: OnlyCatActivitySensorDescription,
        device: Device,
    ) -> None:
        """Initialize the sensor for one flap."""
        super().__init__(coordinator, description, device)
        self._attr_unique_id = (
            device.device_id.replace("-", "_").lower() + "_" + description.key
        )
        self.entity_id = f"sensor.{self._attr_unique_id}"

    def _window(self) -> StatsWindow | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.device(self.device.device_id)


def activity_sensors(
    coordinator: OnlyCatStatsCoordinator,
    devices: list[Device],
    pets: list[Pet],
) -> list[OnlyCatActivitySensor]:
    """Build the per-device and per-chip activity sensors."""
    if not devices:
        return []
    sensors: list[OnlyCatActivitySensor] = [
        OnlyCatDeviceActivitySensor(coordinator, description, device)
        for device in devices
        for description in DEVICE_SENSORS
    ]
    # A chip's counts span every flap; they sit on the first one.
    sensors += [
        OnlyCatChipActivitySensor(coordinator, description, devices[0], pet)
        for pet in pets
        for description in CHIP_SENSORS
    ]
    return sensors
