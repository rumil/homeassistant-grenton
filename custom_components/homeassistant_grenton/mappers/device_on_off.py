"""Mapper for converting OnOff widget DTO to per-component devices."""

from ..coordinator import GrentonCoordinator
from ..domain.devices.on_off import GrentonDeviceOnOff
from ..domain.enums import GrentonActionEventType
from ..domain.action import GrentonAction
from ..domain.state_object import GrentonStateObject
from ..domain.entities.bistable_switch import GrentonEntityBistableSwitch
from ..dto.widgets.on_off import GrentonWidgetOnOffDto


class DeviceOnOffMapper:
    """Map a GrentonWidgetOnOffDto to one HA device per component.

    ON_OFF widgets carry no widget-level label — only each component has one.
    We therefore apply the same split strategy as ON_OFF_DOUBLE: one HA
    device per component, named after its label, with the switch as the
    primary (nameless) entity.  This produces clean entity_ids like
    ``switch.living_room_ceiling`` and avoids falling back to the translated
    type name for the device.
    """

    @staticmethod
    def to_domain(dto: GrentonWidgetOnOffDto, coordinator: GrentonCoordinator) -> list[GrentonDeviceOnOff]:
        devices: list[GrentonDeviceOnOff] = []

        for component in dto.components:
            action_on: GrentonAction | None = None
            action_off: GrentonAction | None = None
            for action_dto in component.actions or []:
                action = GrentonAction.from_dto(action_dto)
                if action.event == GrentonActionEventType.ON:
                    action_on = action
                elif action.event == GrentonActionEventType.OFF:
                    action_off = action

            if not (action_on and action_off):
                continue

            entity_id = f"{dto.id}_{component.rowId}"
            device = GrentonDeviceOnOff(
                type=dto.type,
                id=entity_id,
                entities=[],
                name=component.label,
            )

            entity = GrentonEntityBistableSwitch(
                coordinator=coordinator,
                id=entity_id,
                unit=component.unit,
                state_object=GrentonStateObject.from_dto(component.state),
                action_on=action_on,
                action_off=action_off,
                device_info=device.device_info,
            )

            device.entities = [entity]
            devices.append(device)

        return devices
