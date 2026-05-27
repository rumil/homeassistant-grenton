"""Mapper for converting ContactSensorDouble widget DTO to per-component devices."""

from ..coordinator import GrentonCoordinator
from ..domain.devices.contact_sensor_double import GrentonDeviceContactSensorDouble
from ..domain.action import GrentonAction
from ..domain.state_object import GrentonStateObject
from ..domain.entities.base import BaseGrentonEntity
from ..domain.entities.binary_sensor import GrentonEntityBinarySensor
from ..domain.entities.button import GrentonEntityButton
from ..dto.widgets.contact_sensor_double import GrentonWidgetContactSensorDoubleDto
from ..dto.components.contact_sensor_double import GrentonComponentContactSensorDoubleDto


class DeviceContactSensorDoubleMapper:
    """Map a GrentonWidgetContactSensorDoubleDto to one HA device per side.

    Each side carries its own user-given label. The binary_sensor is the
    device's primary feature; an optional button sub-entity uses the
    `button` translation_key.
    """

    @staticmethod
    def to_domain(
        dto: GrentonWidgetContactSensorDoubleDto, coordinator: GrentonCoordinator
    ) -> list[GrentonDeviceContactSensorDouble]:
        return [
            DeviceContactSensorDoubleMapper._build_side(
                dto_id=dto.id, dto_type=dto.type, side="left", component=dto.componentLeft, coordinator=coordinator
            ),
            DeviceContactSensorDoubleMapper._build_side(
                dto_id=dto.id, dto_type=dto.type, side="right", component=dto.componentRight, coordinator=coordinator
            ),
        ]

    @staticmethod
    def _build_side(
        dto_id: str,
        dto_type: str,
        side: str,
        component: GrentonComponentContactSensorDoubleDto,
        coordinator: GrentonCoordinator,
    ) -> GrentonDeviceContactSensorDouble:
        device = GrentonDeviceContactSensorDouble(
            type=dto_type,
            id=f"{dto_id}_{side}",
            entities=[],
            name=component.label,
        )

        entities: list[BaseGrentonEntity] = [
            GrentonEntityBinarySensor(
                coordinator=coordinator,
                id=f"{dto_id}_binary_sensor_{side}",
                reversed=component.reverseState,
                state_object=GrentonStateObject.from_dto(component.object.value),
                device_info=device.device_info,
            )
        ]

        if component.object.clickAction is not None:
            entities.append(
                GrentonEntityButton(
                    coordinator=coordinator,
                    id=f"{dto_id}_button_{side}",
                    translation_key="button",
                    action_click=GrentonAction.from_dto(component.object.clickAction),
                    device_info=device.device_info,
                )
            )

        device.entities = entities
        return device
