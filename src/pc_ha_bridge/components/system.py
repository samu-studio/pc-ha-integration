"""CPU / RAM / temperature sensors.

Kept deliberately cheap: temperatures are read straight from the one sysfs hwmon file that was
selected at startup (instead of scanning every sensor each cycle), and NVIDIA GPUs are queried
through NVML in-process instead of spawning nvidia-smi.
"""

import asyncio
import ctypes
import logging
import re
from pathlib import Path

import psutil

from ..bridge import Bridge
from ..config import SystemConfig
from .base import Component

log = logging.getLogger(__name__)

HWMON_ROOT = Path("/sys/class/hwmon")

# (chip, label) preference order for the CPU temperature; label None = first reading of that chip.
CPU_TEMP_CANDIDATES = [
    ("k10temp", "Tctl"),
    ("k10temp", "Tdie"),
    ("zenpower", "Tdie"),
    ("coretemp", "Package id 0"),
    ("cpu_thermal", None),
]
GPU_TEMP_CANDIDATES = [("amdgpu", "edge"), ("amdgpu", None)]

HwmonChips = dict[str, list[tuple[str, Path]]]  # chip name -> [(label, tempN_input path)]


def _natural_key(path: Path) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path.name)]


def scan_hwmon(root: Path = HWMON_ROOT) -> HwmonChips:
    chips: HwmonChips = {}
    for hw in sorted(root.glob("hwmon*"), key=_natural_key):
        try:
            name = (hw / "name").read_text().strip()
        except OSError:
            continue
        for inp in sorted(hw.glob("temp*_input"), key=_natural_key):
            label_file = inp.with_name(inp.name.replace("_input", "_label"))
            try:
                label = label_file.read_text().strip()
            except OSError:
                label = ""
            chips.setdefault(name, []).append((label, inp))
    return chips


def find_temp_sensor(
    chips: HwmonChips, wanted: str | None, candidates: list[tuple[str, str | None]]
) -> Path | None:
    if wanted:
        chip, _, label = wanted.partition("/")
        candidates = [(chip, label or None)]
    for chip, label in candidates:
        for entry_label, path in chips.get(chip, []):
            if label is None or entry_label == label:
                return path
    return None


def read_temp(path: Path) -> float | None:
    try:
        return round(int(path.read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None


class NvmlError(Exception):
    pass


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class Nvml:
    """Minimal ctypes binding to libnvidia-ml (ships with the NVIDIA driver)."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self) -> None:
        try:
            self._lib = ctypes.CDLL("libnvidia-ml.so.1")
        except OSError as e:
            raise NvmlError(str(e)) from e
        self._check(self._lib.nvmlInit_v2())
        self._handle = ctypes.c_void_p()
        self._check(self._lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self._handle)))

    def _check(self, ret: int) -> None:
        if ret != 0:
            raise NvmlError(f"NVML error {ret}")

    def read(self) -> tuple[float, float]:
        temp = ctypes.c_uint()
        self._check(self._lib.nvmlDeviceGetTemperature(
            self._handle, self.NVML_TEMPERATURE_GPU, ctypes.byref(temp)))
        util = _Utilization()
        self._check(self._lib.nvmlDeviceGetUtilizationRates(self._handle, ctypes.byref(util)))
        return float(temp.value), float(util.gpu)

    def close(self) -> None:
        self._lib.nvmlShutdown()


class SystemSensors(Component):
    name = "system"

    def __init__(self, bridge: Bridge, cfg: SystemConfig):
        super().__init__(bridge)
        self.cfg = cfg
        self.cpu_temp: Path | None = None
        self.gpu_temp: Path | None = None
        self.nvml: Nvml | None = None

    async def setup(self) -> None:
        b = self.bridge
        psutil.cpu_percent(None)  # prime: the first call always returns 0.0

        b.add_entity("sensor", "cpu_usage", "CPU-Auslastung", unit_of_measurement="%",
                     state_class="measurement", icon="mdi:cpu-64-bit",
                     suggested_display_precision=0)
        b.add_entity("sensor", "memory_usage", "Arbeitsspeicher", unit_of_measurement="%",
                     state_class="measurement", icon="mdi:memory",
                     suggested_display_precision=0)
        b.add_entity("sensor", "memory_used", "Arbeitsspeicher belegt", unit_of_measurement="GiB",
                     device_class="data_size", state_class="measurement",
                     suggested_display_precision=1)

        self._resolve_temps()
        if self.cpu_temp:
            log.info("CPU temperature from %s", self.cpu_temp)
            self._add_temp("cpu_temperature", "CPU-Temperatur")
        else:
            log.warning("no CPU temperature sensor found (chips: %s)", ", ".join(scan_hwmon()))

        if self.cfg.nvidia:
            try:
                self.nvml = Nvml()
                log.info("NVIDIA GPU via NVML")
            except NvmlError as e:
                log.debug("NVML unavailable: %s", e)
        if self.nvml or self.gpu_temp:
            self._add_temp("gpu_temperature", "GPU-Temperatur")
        if self.nvml:
            b.add_entity("sensor", "gpu_usage", "GPU-Auslastung", unit_of_measurement="%",
                         state_class="measurement", icon="mdi:expansion-card")

    def _resolve_temps(self) -> None:
        chips = scan_hwmon()
        self.cpu_temp = find_temp_sensor(chips, self.cfg.cpu_temp_sensor, CPU_TEMP_CANDIDATES)
        self.gpu_temp = find_temp_sensor(chips, None, GPU_TEMP_CANDIDATES)

    def _add_temp(self, object_id: str, name: str) -> None:
        self.bridge.add_entity("sensor", object_id, name, unit_of_measurement="°C",
                               device_class="temperature", state_class="measurement",
                               suggested_display_precision=0)

    def _read_temps(self) -> tuple[float | None, float | None]:
        cpu = read_temp(self.cpu_temp) if self.cpu_temp else None
        gpu = read_temp(self.gpu_temp) if self.gpu_temp else None
        # hwmon numbering can change when drivers reload; re-resolve once if a read failed.
        if (self.cpu_temp and cpu is None) or (self.gpu_temp and gpu is None):
            self._resolve_temps()
            cpu = read_temp(self.cpu_temp) if self.cpu_temp else None
            gpu = read_temp(self.gpu_temp) if self.gpu_temp else None
        return cpu, gpu

    async def run(self) -> None:
        b = self.bridge
        while True:
            b.publish_state("cpu_usage", round(psutil.cpu_percent(None)))
            mem = psutil.virtual_memory()
            b.publish_state("memory_usage", round(mem.percent))
            b.publish_state("memory_used", round((mem.total - mem.available) / 2**30, 1))

            cpu, gpu = self._read_temps()
            if self.cpu_temp:
                b.publish_state("cpu_temperature", cpu)
            if self.nvml:
                try:
                    gpu, usage = await asyncio.wait_for(asyncio.to_thread(self.nvml.read), 5)
                except (NvmlError, TimeoutError) as e:
                    log.debug("NVML read failed: %s", e)
                    gpu = usage = None
                b.publish_state("gpu_usage", usage)
            if self.nvml or self.gpu_temp:
                b.publish_state("gpu_temperature", gpu)

            await asyncio.sleep(self.cfg.interval)

    async def close(self) -> None:
        if self.nvml:
            self.nvml.close()
