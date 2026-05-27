from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import callback

from ...coordinator import GrentonCoordinator


class BaseGrentonEntity(CoordinatorEntity[GrentonCoordinator]):
    """Base entity class that inherits CoordinatorEntity for automatic updates.

    Naming follows HA's `has_entity_name` convention:
      - `name=None` and no `translation_key` → entity is the device's primary
        feature; HA uses the device name as the entity name.
      - `name="..."` → user-defined name from the Grenton API; not translatable.
      - `translation_key="..."` → fixed sub-feature; HA resolves the translation
        from `entity.<platform>.<translation_key>.name` in the strings file.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GrentonCoordinator,
        id: str,
        name: str | None = None,
        translation_key: str | None = None,
        device_info: DeviceInfo | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = id
        self._attr_name = name
        if translation_key is not None:
            self._attr_translation_key = translation_key
        self._attr_device_info = device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
