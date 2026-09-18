"""Mapper for converting ValueDouble widget DTO to domain device."""

from ..coordinator import GrentonCoordinator
from ..domain.devices.roller_shutter import GrentonDeviceRollerShutter
from ..domain.state_object import GrentonStateObject
from ..domain.action import GrentonAction
from ..domain.entities.roller_shutter_sensor import GrentonEntityRollerShutterSensor
from ..domain.entities.button import GrentonEntityButton
from ..dto.widgets.roller_shutter import GrentonWidgetRollerShutterDto


class DeviceRollerShutterMapper:
    """Mapper for GrentonWidgetRollerShutterDto to GrentonDeviceRollerShutter."""

    @staticmethod
    def to_domain(dto: GrentonWidgetRollerShutterDto, coordinator: GrentonCoordinator) -> list[GrentonDeviceRollerShutter]:
        """Convert DTO to a single-element list of domain devices."""
        device = GrentonDeviceRollerShutter(
            type=dto.type,
            id=dto.id,
            entities=[],
            name=dto.components[0].label,
        )

        entity_sensor = GrentonEntityRollerShutterSensor(
            coordinator=coordinator,
            id=f"{dto.id}_sensor",
            state_object=GrentonStateObject.from_dto(dto.components[0].state),
            device_info=device.device_info,
        )

        entity_button = GrentonEntityButton(
            coordinator=coordinator,
            id=f"{dto.id}_button",
            translation_key="button",
            action_click=GrentonAction.from_dto(dto.components[0].actions[0]),
            device_info=device.device_info,
        )

        device.entities = [entity_sensor, entity_button]
        return [device]
