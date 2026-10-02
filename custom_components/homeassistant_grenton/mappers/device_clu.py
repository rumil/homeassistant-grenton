"""Mapper creating one diagnostic device per CLU."""

from ..coordinator import GrentonCoordinator
from ..domain.clu import GrentonClu
from ..domain.devices.clu import GrentonDeviceClu
from ..domain.entities.clu_connectivity import GrentonEntityCluConnectivity


class DeviceCluMapper:
    """Map the interface's CLUs to devices with a connectivity sensor.

    With a single CLU the device is named "Grenton CLU", so its sensor is
    ``binary_sensor.grenton_clu_connectivity``. With several CLUs the serial
    number is added to keep names unique.
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
            entity = GrentonEntityCluConnectivity(
                coordinator=coordinator,
                id=f"clu_{clu.id}_connectivity",
                clu_id=clu.id,
                device_info=device.device_info,
            )
            device.entities = [entity]  # type: ignore[list-item]
            devices.append(device)

        return devices
