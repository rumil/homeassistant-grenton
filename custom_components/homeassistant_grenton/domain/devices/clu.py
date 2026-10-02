from dataclasses import dataclass

from homeassistant.helpers.device_registry import DeviceInfo

from .base import BaseGrentonDevice


@dataclass
class GrentonDeviceClu(BaseGrentonDevice):
    """Device for a CLU itself, holding its diagnostic entities."""

    serial_number: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        info = super().device_info
        if self.serial_number:
            info["serial_number"] = self.serial_number
        return info
