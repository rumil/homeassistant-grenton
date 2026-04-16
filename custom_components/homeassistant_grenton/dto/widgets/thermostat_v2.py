from __future__ import annotations
from typing import Literal

from .base import GrentonWidgetDto
from ..objects.thermostat_v2 import GrentonObjectThermostatV2Dto


class GrentonWidgetThermostatV2Dto(GrentonWidgetDto):
    type: Literal["THERMOSTAT_V2"] = "THERMOSTAT_V2"
    label: str
    icon: str
    no_of_fan_speeds: int
    object: GrentonObjectThermostatV2Dto