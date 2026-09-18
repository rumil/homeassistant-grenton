"""Mapper for converting ValueV2 widget DTO to domain device."""

from ..const import EVENT_PAGE_NAME
from ..coordinator import GrentonCoordinator
from ..domain.devices.value_v2 import GrentonDeviceValueV2
from ..domain.state_object import GrentonStateObject
from ..domain.entities.value import GrentonEntityValue
from ..domain.entities.gesture_event import GrentonEntityGestureEvent
from ..dto.widgets.value_v2 import GrentonWidgetValueV2Dto


def _is_event_page(page_name: str | None) -> bool:
    """Whether a page name selects gesture event entities (case-insensitive)."""
    if page_name is None:
        return False
    return page_name.strip().casefold() == EVENT_PAGE_NAME.strip().casefold()


class DeviceValueV2Mapper:
    """Mapper for GrentonWidgetValueV2Dto to GrentonDeviceValueV2."""

    @staticmethod
    def to_domain(
        dto: GrentonWidgetValueV2Dto,
        coordinator: GrentonCoordinator,
        page_name: str | None = None,
    ) -> list[GrentonDeviceValueV2]:
        """Convert DTO to a single-element list of domain devices.

        On a page named after ``EVENT_PAGE_NAME`` the widget becomes a gesture
        ``event`` entity; on any other page it stays a ``sensor`` as before. The
        device carries the widget label as its name either way.
        """
        device = GrentonDeviceValueV2(
            type=dto.type,
            id=dto.id,
            entities=[],
            name=dto.label,
        )

        state_object = GrentonStateObject.from_dto(dto.object.value)

        if _is_event_page(page_name):
            entity = GrentonEntityGestureEvent(
                coordinator=coordinator,
                id=f"{dto.id}_gesture",
                state_object=state_object,
                device_info=device.device_info,
            )
        else:
            entity = GrentonEntityValue(
                coordinator=coordinator,
                id=f"{dto.id}_0",
                state_object=state_object,
                device_info=device.device_info,
            )

        device.entities = [entity]
        return [device]
