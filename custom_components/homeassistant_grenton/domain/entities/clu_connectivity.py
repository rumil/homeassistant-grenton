from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify

from ...coordinator import GrentonCoordinator

ATTR_LAST_CONTACT = "last_contact"

# While the connectivity state does not change, the last_contact attribute is
# written at most this often. Every ping response is a contact, and writing
# the state on each one would add a database row every few seconds.
LAST_CONTACT_REFRESH_INTERVAL = timedelta(minutes=5)


class GrentonEntityCluConnectivity(BinarySensorEntity):
    """Diagnostic connectivity sensor of one CLU (on = connected).

    It is not a CoordinatorEntity: it must stay available while the CLU is
    disconnected, and it only writes state on connectivity changes, not on
    every report from the CLU.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({ATTR_LAST_CONTACT})

    def __init__(
        self,
        coordinator: GrentonCoordinator,
        id: str,
        clu_id: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        # No name: HA names the entity by its device class ("Connectivity",
        # translated in the UI). HA derives the entity_id from that translated
        # name, so it is suggested explicitly to keep it stable across
        # languages (e.g. binary_sensor.grenton_clu_connectivity). HA uses it
        # only on first registration; the entity registry keeps it after that.
        device_name = device_info.get("name") if device_info else None
        if device_name:
            self.entity_id = f"binary_sensor.{slugify(device_name)}_connectivity"
        self.coordinator = coordinator
        self.clu_id = clu_id
        self._attr_unique_id = id
        self._attr_device_info = device_info
        self._written_connected: bool | None = None
        self._written_last_contact: datetime | None = None

    @property
    def is_on(self) -> bool | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        connectivity = self.coordinator.get_connectivity(self.clu_id)
        return connectivity.connected if connectivity else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleVariableOverride]
        connectivity = self.coordinator.get_connectivity(self.clu_id)
        return {ATTR_LAST_CONTACT: connectivity.last_contact if connectivity else None}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_connectivity_listener(self.clu_id, self._handle_connectivity_update)
        )
        self._remember_written()

    @callback
    def _remember_written(self) -> None:
        connectivity = self.coordinator.get_connectivity(self.clu_id)
        self._written_connected = connectivity.connected if connectivity else None
        self._written_last_contact = connectivity.last_contact if connectivity else None

    @callback
    def _handle_connectivity_update(self) -> None:
        connectivity = self.coordinator.get_connectivity(self.clu_id)
        if connectivity is None:
            return
        last_contact = connectivity.last_contact
        contact_due = last_contact is not None and (
            self._written_last_contact is None
            or last_contact - self._written_last_contact >= LAST_CONTACT_REFRESH_INTERVAL
        )
        if connectivity.connected == self._written_connected and not contact_due:
            return
        self._remember_written()
        self.async_write_ha_state()
