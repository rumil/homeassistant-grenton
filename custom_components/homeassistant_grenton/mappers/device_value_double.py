"""Mapper for converting ValueDouble widget DTO to per-component devices."""

from ..coordinator import GrentonCoordinator
from ..domain.devices.value_double import GrentonDeviceValueDouble
from ..domain.state_object import GrentonStateObject
from ..domain.entities.value import GrentonEntityValue
from ..dto.widgets.value_double import GrentonWidgetValueDoubleDto


class DeviceValueDoubleMapper:
    """Map a GrentonWidgetValueDoubleDto to one HA device per component.

    Each side of a VALUE_DOUBLE carries its own user-given label, so we split
    into two standalone HA devices, each with the value sensor as its primary
    feature.
    """

    @staticmethod
    def to_domain(dto: GrentonWidgetValueDoubleDto, coordinator: GrentonCoordinator) -> list[GrentonDeviceValueDouble]:
        device_left = GrentonDeviceValueDouble(
            type=dto.type,
            id=f"{dto.id}_0",
            entities=[],
            name=dto.componentLeft.label,
        )
        entity_left = GrentonEntityValue(
            coordinator=coordinator,
            id=f"{dto.id}_0",
            state_object=GrentonStateObject.from_dto(dto.componentLeft.object.value),
            device_info=device_left.device_info,
        )
        device_left.entities = [entity_left]

        device_right = GrentonDeviceValueDouble(
            type=dto.type,
            id=f"{dto.id}_1",
            entities=[],
            name=dto.componentRight.label,
        )
        entity_right = GrentonEntityValue(
            coordinator=coordinator,
            id=f"{dto.id}_1",
            state_object=GrentonStateObject.from_dto(dto.componentRight.object.value),
            device_info=device_right.device_info,
        )
        device_right.entities = [entity_right]

        return [device_left, device_right]
