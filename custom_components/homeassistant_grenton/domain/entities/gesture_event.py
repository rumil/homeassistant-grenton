from homeassistant.components.event import EventEntity, EventDeviceClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.core import callback

from .base import BaseGrentonEntity
from ..state_object import GrentonStateObject
from ..gesture_decoder import GestureDecoder, ORIGIN_RESYNC
from ...coordinator import GrentonCoordinator


class GrentonEntityGestureEvent(BaseGrentonEntity, EventEntity):
    """Event entity for a Grenton gesture channel.

    The channel is a numeric User Feature shown as a VALUE_V2 widget. Its value
    encodes discrete gestures (single/double/hold_start/hold_release), decoded by
    the pure ``GestureDecoder``. The device carries the widget label as its name;
    this entity is a translated "Gesture" sub-feature. Its state changes only
    through the dedicated gesture listener, never on ordinary coordinator updates.
    """

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = ["single", "double", "hold_start", "hold_release"]

    def __init__(
        self,
        coordinator: GrentonCoordinator,
        id: str,
        state_object: GrentonStateObject,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize gesture event entity."""
        # Primary entity of its device (name=None), so it inherits the device's
        # label as its name. The translation_key carries only the event_type
        # state translations (entity.event.gesture has no "name" key).
        BaseGrentonEntity.__init__(self, coordinator, id, None, "gesture", device_info)
        EventEntity.__init__(self)

        self.state_object = state_object
        self._decoder = GestureDecoder(name=id)

        # Register state with coordinator so the key is subscribed and included
        # in register/report cycles.
        coordinator.register_component_state(state_object)

    async def async_added_to_hass(self) -> None:
        """Register the gesture listener and seed the decoder baseline."""
        await super().async_added_to_hass()

        # Listen for value updates with their origin; removed automatically on
        # entity removal.
        self.async_on_remove(
            self.coordinator.async_add_gesture_listener(
                self.state_object, self._handle_gesture
            )
        )

        # Seed the baseline from the current value as a resync (never fires) so a
        # value left over from before startup does not fire a stale gesture.
        current = self.coordinator.get_value_for_component(self.state_object)
        self._decoder.decode(current, ORIGIN_RESYNC)

    @callback
    def _handle_coordinator_update(self) -> None:
        """No-op: gesture state changes only via the gesture listener."""
        return

    @callback
    def _handle_gesture(self, value, origin: str) -> None:
        """Decode a value update and fire an event if a gesture is detected."""
        gesture = self._decoder.decode(value, origin)
        if gesture is None:
            return
        self._trigger_event(gesture.event_type, {"sequence": gesture.sequence})
        self.async_write_ha_state()
