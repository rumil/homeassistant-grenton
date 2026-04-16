from __future__ import annotations
from pydantic import BaseModel

from ..value import GrentonValueUnionDto
from ..action import GrentonActionUnionDto


class GrentonObjectThermostatV2Dto(BaseModel):
    currentTemperature: GrentonValueUnionDto
    targetTemperature: GrentonValueUnionDto
    minTemperature: GrentonValueUnionDto
    maxTemperature: GrentonValueUnionDto
    state: GrentonValueUnionDto
    controlOutValue: GrentonValueUnionDto
    mode: GrentonValueUnionDto
    scheduleData: GrentonValueUnionDto
    setTargetTemperatureAction: GrentonActionUnionDto
    setStateAction: GrentonActionUnionDto
    setModeAction: GrentonActionUnionDto
    setScheduleDataAction: GrentonActionUnionDto
    setMinTemperatureAction: GrentonActionUnionDto
    setMaxTemperatureAction: GrentonActionUnionDto