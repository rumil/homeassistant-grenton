"""Mapper for converting OnOffDouble widget DTO to per-component devices."""

from ..coordinator import GrentonCoordinator
from ..domain.devices.on_off_double import GrentonDeviceOnOffDouble
from ..domain.enums import GrentonActionEventType
from ..domain.action import GrentonAction
from ..domain.state_object import GrentonStateObject
from ..domain.entities.bistable_switch import GrentonEntityBistableSwitch
from ..dto.widgets.on_off_double import GrentonWidgetOnOffDoubleDto


class DeviceOnOffDoubleMapper:
    """Map a GrentonWidgetOnOffDoubleDto to one HA device per component.

    Each side of an ON_OFF_DOUBLE has its own user-given label in Grenton, so
    we surface each as a standalone HA device with the switch as its primary
    feature. This produces clean entity_ids like `switch.living_room_ceiling_1`.
    """

    @staticmethod
    def to_domain(dto: GrentonWidgetOnOffDoubleDto, coordinator: GrentonCoordinator) -> list[GrentonDeviceOnOffDouble]:
        devices: list[GrentonDeviceOnOffDouble] = []

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
            device = GrentonDeviceOnOffDouble(
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
