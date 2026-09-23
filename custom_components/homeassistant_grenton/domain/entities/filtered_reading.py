import time
from typing import Any, Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.const import DEVICE_CLASS_STATE_CLASSES
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from ..noise_filter import NoiseFilter, NoiseFilterParams
from ..state_object import GrentonStateObject

# Noise filtering per sensor device class. Temperatures are reported with 0.1
# resolution and flicker by one step around rounding boundaries; a 0.2 deadband
# passes real changes immediately while one-step flicker must hold for a minute.
NOISE_FILTER_PARAMS: dict[SensorDeviceClass, NoiseFilterParams] = {
    SensorDeviceClass.TEMPERATURE: NoiseFilterParams(deadband=0.2, settle_seconds=60),
}


def measurement_state_class(device_class: SensorDeviceClass | None) -> SensorStateClass | None:
    """Return MEASUREMENT when the device class allows it, else None."""
    if device_class is None:
        return None
    if SensorStateClass.MEASUREMENT in DEVICE_CLASS_STATE_CLASSES.get(device_class, set()):
        return SensorStateClass.MEASUREMENT
    return None


class FilteredReading:
    """Noise-filtered view of one raw Grenton value.

    Wraps the pure ``NoiseFilter`` with the Home Assistant timer that publishes
    a small change once it has held for the settle time, even when no further
    report arrives. With ``params=None`` the raw value passes through unchanged.
    """

    def __init__(self, read_raw: Callable[[], Any], on_change: Callable[[], None]) -> None:
        self._read_raw = read_raw
        self._on_change = on_change
        self._filter: NoiseFilter | None = None
        self._hass: HomeAssistant | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._timer_deadline: float | None = None

    @property
    def value(self) -> Any:
        if self._filter is None:
            return self._read_raw()
        return self._filter.published

    @callback
    def refresh(self, hass: HomeAssistant, params: NoiseFilterParams | None) -> None:
        """Feed the current raw value through the filter (call on every update)."""
        self._hass = hass
        if params is None:
            self._filter = None
            self.cancel()
            return
        if self._filter is None or self._filter.params != params:
            self._filter = NoiseFilter(params)
        self._filter.offer(self._read_raw(), time.monotonic())
        self._schedule()

    @callback
    def cancel(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
        self._unsub_timer = None
        self._timer_deadline = None

    @callback
    def _schedule(self) -> None:
        deadline = self._filter.pending_deadline if self._filter else None
        if deadline == self._timer_deadline:
            return
        self.cancel()
        if deadline is None or self._hass is None:
            return
        self._timer_deadline = deadline
        self._unsub_timer = async_call_later(
            self._hass, max(0.0, deadline - time.monotonic()), self._handle_deadline
        )

    @callback
    def _handle_deadline(self, _now: Any) -> None:
        self._unsub_timer = None
        self._timer_deadline = None
        if self._filter is None:
            return
        if self._filter.offer(self._read_raw(), time.monotonic()):
            self._on_change()
        self._schedule()


def raw_numeric_value(coordinator: Any, state_object: GrentonStateObject) -> Any:
    """Read a sensor value, mapping the "not yet received" placeholder to None."""
    value = coordinator.get_value_for_component(state_object)
    return None if value == "" else value


class NoiseFilteredSensorMixin:
    """Serve ``native_value`` through a ``FilteredReading`` keyed by device class.

    Place before ``BaseGrentonEntity`` in the bases. Requires ``state_object``.
    """

    coordinator: Any
    hass: HomeAssistant
    state_object: GrentonStateObject
    device_class: SensorDeviceClass | None
    _noise_reading: FilteredReading | None = None

    @property
    def _reading(self) -> FilteredReading:
        if self._noise_reading is None:
            self._noise_reading = FilteredReading(
                lambda: raw_numeric_value(self.coordinator, self.state_object),
                self.async_write_ha_state,  # type: ignore[attr-defined]
            )
        return self._noise_reading

    @callback
    def _refresh_reading(self) -> None:
        params = NOISE_FILTER_PARAMS.get(self.device_class) if self.device_class else None
        self._reading.refresh(self.hass, params)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()  # type: ignore[misc]
        self.async_on_remove(self._reading.cancel)  # type: ignore[attr-defined]
        # Seed before the initial state write so it is not "unknown".
        self._refresh_reading()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_reading()
        super()._handle_coordinator_update()  # type: ignore[misc]

    @property
    def native_value(self):  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._reading.value

    @property
    def state_class(self) -> SensorStateClass | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        return measurement_state_class(self.device_class)
