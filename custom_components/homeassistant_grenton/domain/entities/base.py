from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import callback

from ...coordinator import GrentonCoordinator
from ..action import GrentonAction
from ..state_object import GrentonStateObject
from ..utils.naming import normalize_label


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

    # CLUs this entity reads from or acts on; see _clu_ids.
    _grenton_clu_ids: frozenset[str] | None = None

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
        self._attr_name = normalize_label(name)
        if translation_key is not None:
            self._attr_translation_key = translation_key
        self._attr_device_info = device_info

    @property
    def _clu_ids(self) -> frozenset[str]:
        """CLU ids of the state objects and actions held by this entity.

        Collected from the instance attributes on first use, so subclasses do
        not have to declare them. Entities without any (e.g. camera) do not
        depend on CLU connectivity.
        """
        if self._grenton_clu_ids is None:
            self._grenton_clu_ids = frozenset(
                value.clu_id
                for value in vars(self).values()
                if isinstance(value, (GrentonStateObject, GrentonAction))
            )
        return self._grenton_clu_ids

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Unavailable while any CLU of this entity is not responding or not resynced."""
        return super().available and all(
            self.coordinator.is_clu_available(clu_id) for clu_id in self._clu_ids
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
