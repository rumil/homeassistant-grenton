from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify

from ...coordinator import GrentonCoordinator


class GrentonEntityCluLastBoot(SensorEntity):
    """Diagnostic sensor: when the CLU last booted.

    The value is read from the CLU's uptime when the integration starts, and
    changes only when a restart of the CLU is detected, at the same moment
    the ``grenton_clu_restarted`` event is fired: after the CLU's state was
    read again. Unknown until the CLU answered its first uptime query (and
    for good on a CLU that does not report its uptime).

    Like the connectivity sensor it is not a CoordinatorEntity: it stays
    available while the CLU is not responding, showing the last known boot.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_boot"

    def __init__(
        self,
        coordinator: GrentonCoordinator,
        id: str,
        clu_id: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        # HA derives the entity_id from the translated name, so it is
        # suggested explicitly to keep it stable across languages, e.g.
        # sensor.grenton_clu_last_boot. Used only on first registration.
        device_name = device_info.get("name") if device_info else None
        if device_name:
            self.entity_id = f"sensor.{slugify(device_name)}_last_boot"
        self.coordinator = coordinator
        self.clu_id = clu_id
        self._attr_unique_id = id
        self._attr_device_info = device_info

    @property
    def native_value(self) -> datetime | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self.coordinator.get_boot_time(self.clu_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_boot_listener(self.clu_id, self._handle_boot_time_update)
        )

    @callback
    def _handle_boot_time_update(self) -> None:
        self.async_write_ha_state()
