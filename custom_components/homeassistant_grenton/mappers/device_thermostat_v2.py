"""Mapper for converting ThermostatV2 widget DTO to domain device."""

from ..coordinator import GrentonCoordinator
from ..domain.devices.thermostat_v2 import GrentonDeviceThermostatV2
from ..domain.state_object import GrentonStateObject
from ..domain.action import GrentonAction
from ..domain.entities.climate import GrentonEntityClimate
from ..dto.widgets.thermostat_v2 import GrentonWidgetThermostatV2Dto


class DeviceThermostatV2Mapper:
    """Mapper for GrentonWidgetThermostatV2Dto to GrentonDeviceThermostatV2."""

    @staticmethod
    def to_domain(dto: GrentonWidgetThermostatV2Dto, coordinator: GrentonCoordinator) -> GrentonDeviceThermostatV2:
        """Convert DTO to domain object."""
        device = GrentonDeviceThermostatV2(
            type=dto.type,
            id=dto.id,
            entities=[],
        )

        entity = GrentonEntityClimate(
            coordinator=coordinator,
            id=f"{dto.id}_0",
            label=dto.label,
            state_current_temperature=GrentonStateObject.from_dto(dto.object.currentTemperature),
            state_target_temperature=GrentonStateObject.from_dto(dto.object.targetTemperature),
            state_min_temperature=GrentonStateObject.from_dto(dto.object.minTemperature),
            state_max_temperature=GrentonStateObject.from_dto(dto.object.maxTemperature),
            state_on_off=GrentonStateObject.from_dto(dto.object.state),
            state_control_out=GrentonStateObject.from_dto(dto.object.controlOutValue),
            state_mode=GrentonStateObject.from_dto(dto.object.mode),
            action_set_target_temperature=GrentonAction.from_dto(dto.object.setTargetTemperatureAction),
            action_set_state=GrentonAction.from_dto(dto.object.setStateAction),
            action_set_mode=GrentonAction.from_dto(dto.object.setModeAction),
            device_info=device.device_info,
        )

        device.entities = [entity]
        return device