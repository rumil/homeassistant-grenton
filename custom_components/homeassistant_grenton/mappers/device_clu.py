"""Mapper creating one diagnostic device per CLU."""

from ..coordinator import GrentonCoordinator
from ..domain.clu import GrentonClu
from ..domain.devices.clu import GrentonDeviceClu
from ..domain.entities.clu_connectivity import GrentonEntityCluConnectivity
from ..domain.entities.clu_last_boot import GrentonEntityCluLastBoot


class DeviceCluMapper:
    """Map the interface's CLUs to devices with diagnostic sensors.

    Each CLU device has a connectivity sensor and a last-boot sensor. With a
    single CLU the device is named "Grenton CLU", so they are
    ``binary_sensor.grenton_clu_connectivity`` and
    ``sensor.grenton_clu_last_boot``. With several CLUs the serial number is
    added to keep names unique.
    """

    @staticmethod
    def to_domain(clus: list[GrentonClu], coordinator: GrentonCoordinator) -> list[GrentonDeviceClu]:
        devices: list[GrentonDeviceClu] = []

        for clu in clus:
            name = "Grenton CLU" if len(clus) == 1 else f"Grenton CLU {clu.serial_number}"
            device = GrentonDeviceClu(
                type="CLU",
                id=f"clu_{clu.id}",
                entities=[],
                name=name,
                serial_number=clu.serial_number,
            )
            connectivity = GrentonEntityCluConnectivity(
                coordinator=coordinator,
                id=f"clu_{clu.id}_connectivity",
                clu_id=clu.id,
                device_info=device.device_info,
            )
            last_boot = GrentonEntityCluLastBoot(
                coordinator=coordinator,
                id=f"clu_{clu.id}_last_boot",
                clu_id=clu.id,
                device_info=device.device_info,
            )
            device.entities = [connectivity, last_boot]  # type: ignore[list-item]
            devices.append(device)

        return devices
