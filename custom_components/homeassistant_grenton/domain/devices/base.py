from abc import ABC
from dataclasses import dataclass, field

from homeassistant.helpers.device_registry import DeviceInfo

from ..entities.base import BaseGrentonEntity


@dataclass
class BaseGrentonDevice(ABC):
    type: str
    id: str
    entities: list[BaseGrentonEntity]
    name: str | None = field(default=None)

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device info.

        When `name` is set (user-defined label from the Grenton API), it is used
        directly. Otherwise, fall back to the translated widget-type name via
        `translation_key`.
        """
        info: DeviceInfo = DeviceInfo(
            identifiers={("grenton", self.id)},
            manufacturer="Grenton",
            model=self.type,
        )
        if self.name is not None:
            info["name"] = self.name
        else:
            info["translation_key"] = self.type.lower()
        return info
