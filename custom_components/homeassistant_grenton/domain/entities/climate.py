from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode, HVACAction
from homeassistant.components.climate.const import PRESET_AWAY, PRESET_NONE
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo

from .base import BaseGrentonEntity
from ..action import GrentonAction
from ..state_object import GrentonStateObject
from ...coordinator import GrentonCoordinator

PRESET_SCHEDULE = "schedule"

GRENTON_MODE_MANUAL = 0
GRENTON_MODE_AWAY = 1
GRENTON_MODE_SCHEDULE = 2

PRESET_TO_GRENTON_MODE = {
    PRESET_NONE: GRENTON_MODE_MANUAL,
    PRESET_AWAY: GRENTON_MODE_AWAY,
    PRESET_SCHEDULE: GRENTON_MODE_SCHEDULE,
}

GRENTON_MODE_TO_PRESET = {v: k for k, v in PRESET_TO_GRENTON_MODE.items()}


class GrentonEntityClimate(BaseGrentonEntity, ClimateEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Thermostat V2 climate entity."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_preset_modes = [PRESET_NONE, PRESET_AWAY, PRESET_SCHEDULE]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: GrentonCoordinator,
        id: str,
        label: str,
        state_current_temperature: GrentonStateObject,
        state_target_temperature: GrentonStateObject,
        state_min_temperature: GrentonStateObject,
        state_max_temperature: GrentonStateObject,
        state_on_off: GrentonStateObject,
        state_control_out: GrentonStateObject,
        state_mode: GrentonStateObject,
        action_set_target_temperature: GrentonAction,
        action_set_state: GrentonAction,
        action_set_mode: GrentonAction,
        device_info: DeviceInfo | None = None,
    ) -> None:
        ClimateEntity.__init__(self)
        BaseGrentonEntity.__init__(self, coordinator, id, label, device_info)
        self._state_current_temperature = state_current_temperature
        self._state_target_temperature = state_target_temperature
        self._state_min_temperature = state_min_temperature
        self._state_max_temperature = state_max_temperature
        self._state_on_off = state_on_off
        self._state_control_out = state_control_out
        self._state_mode = state_mode
        self._action_set_target_temperature = action_set_target_temperature
        self._action_set_state = action_set_state
        self._action_set_mode = action_set_mode

        coordinator.register_component_state(state_current_temperature)
        coordinator.register_component_state(state_target_temperature)
        coordinator.register_component_state(state_min_temperature)
        coordinator.register_component_state(state_max_temperature)
        coordinator.register_component_state(state_on_off)
        coordinator.register_component_state(state_control_out)
        coordinator.register_component_state(state_mode)

    def _get_float(self, state_object: GrentonStateObject) -> float | None:
        value = self.coordinator.get_value_for_component(state_object)
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    @property
    def current_temperature(self) -> float | None:
        return self._get_float(self._state_current_temperature)

    @property
    def target_temperature(self) -> float | None:
        """Return TargetTemp — the active setpoint regardless of mode."""
        return self._get_float(self._state_target_temperature)

    @property
    def min_temp(self) -> float:
        value = self._get_float(self._state_min_temperature)
        return value if value is not None else 5.0

    @property
    def max_temp(self) -> float:
        value = self._get_float(self._state_max_temperature)
        return value if value is not None else 35.0

    @property
    def hvac_mode(self) -> HVACMode | None:
        value = self._get_float(self._state_on_off)
        if value is None:
            return None
        return HVACMode.OFF if value == 0 else HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        state = self._get_float(self._state_on_off)
        if state is None:
            return None
        if state == 0:
            return HVACAction.OFF
        control_out = self._get_float(self._state_control_out)
        if control_out is not None and control_out > 0:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        value = self._get_float(self._state_mode)
        if value is None:
            return None
        return GRENTON_MODE_TO_PRESET.get(int(value), PRESET_NONE)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature and switch to manual mode."""
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        self._action_set_target_temperature.value = str(temperature)
        await self.coordinator.execute_action(self._action_set_target_temperature)
        self._action_set_mode.value = str(GRENTON_MODE_MANUAL)
        await self.coordinator.execute_action(self._action_set_mode)

    async def async_turn_on(self) -> None:
        self._action_set_state.value = "1"
        await self.coordinator.execute_action(self._action_set_state)

    async def async_turn_off(self) -> None:
        self._action_set_state.value = "0"
        await self.coordinator.execute_action(self._action_set_state)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._action_set_state.value = "0" if hvac_mode == HVACMode.OFF else "1"
        await self.coordinator.execute_action(self._action_set_state)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        grenton_mode = PRESET_TO_GRENTON_MODE.get(preset_mode, GRENTON_MODE_MANUAL)
        self._action_set_mode.value = str(grenton_mode)
        await self.coordinator.execute_action(self._action_set_mode)