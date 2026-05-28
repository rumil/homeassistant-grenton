import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr

from .integration_config import GrentonConfigEntry, GrentonConfigEntryData, RuntimeData
from .coordinator import GrentonCoordinator
from .mappers.device_mapper import DeviceMapper

from .dto.mobile_interface import GrentonMobileInterfaceDto
from .domain.encryption import GrentonEncryption
from .domain.clu import GrentonClu

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.SENSOR, Platform.LIGHT, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.COVER, Platform.CAMERA, Platform.CLIMATE]

async def async_setup_entry(hass: HomeAssistant, config_entry: GrentonConfigEntry) -> bool:
    _LOGGER.debug("Initializing Home Assistant Grenton integration")
    
    config_data: GrentonConfigEntryData = config_entry.data # type: ignore

    mobile_interface_dto = GrentonMobileInterfaceDto(**config_data["interface"])
    
    # Create coordinator first (before devices need it)
    encryption = GrentonEncryption.from_dto(mobile_interface_dto.encryption)
    clus = [GrentonClu.from_dto(clu) for clu in mobile_interface_dto.clus]
    coordinator = GrentonCoordinator(hass, config_entry, clus, encryption)
    
    _LOGGER.debug("Loaded interface with %d CLU(s)", len(clus))
    _LOGGER.debug("CLU details:")
    for clu in clus:
        _LOGGER.debug("- CLU %s (%s) at %s:%d", clu.name, clu.serial_number, clu.ip, clu.port)
    
    # Map mobile interface DTO to devices
    devices = DeviceMapper.from_mobile_interface(mobile_interface_dto, coordinator)

    _LOGGER.debug("Mapped %d device(s) from mobile interface", len(devices))
    _LOGGER.debug("Device details:")
    for device in devices:
        _LOGGER.debug("- Device %s (%s) with %d entity(ies)", device.type, device.id, len(device.entities))
        _LOGGER.debug("  Entities:")
        for entity in device.entities:
            _LOGGER.debug("  - Entity %s", entity.name)
    
    # Store runtime data
    config_entry.runtime_data = RuntimeData(coordinator=coordinator, devices=devices)

    # Reconcile the device registry against the freshly-mapped devices.
    # Anything still linked to this config entry but no longer present in the
    # current device set is a leftover (e.g. devices created before the
    # _DOUBLE widget split, or components removed from the Grenton config).
    _cleanup_stale_devices(hass, config_entry, devices)

    # Setup the coordinator
    await coordinator.async_setup()

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    return True


def _cleanup_stale_devices(
    hass: HomeAssistant,
    config_entry: GrentonConfigEntry,
    devices: list,
) -> None:
    """Remove device-registry entries no longer produced by the mappers."""
    dev_reg = dr.async_get(hass)
    expected = {("grenton", device.id) for device in devices}
    for dev in dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id):
        if not dev.identifiers & expected:
            _LOGGER.info(
                "Removing stale Grenton device %s (identifiers=%s)",
                dev.id,
                dev.identifiers,
            )
            dev_reg.async_update_device(
                dev.id, remove_config_entry_id=config_entry.entry_id
            )


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow users to manually delete a Grenton device from the UI.

    Acts as a safety net for any orphan the automatic cleanup misses.
    """
    return True

async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    coordinator: GrentonCoordinator = config_entry.runtime_data.coordinator
    
    # Shutdown coordinator (close UDP sockets)
    await coordinator.async_shutdown()
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    
    return unload_ok