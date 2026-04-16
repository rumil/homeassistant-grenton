from dataclasses import dataclass

from .base import BaseGrentonDevice


@dataclass
class GrentonDeviceThermostatV2(BaseGrentonDevice):
    """Device for Thermostat V2 widgets."""