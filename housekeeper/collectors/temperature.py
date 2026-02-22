"""Temperature & Fan collector - /sys/class/hwmon から各種温度・ファンセンサーを取得。

対応:
  - CPU: k10temp (AMD), coretemp (Intel)
  - NVMe: nvme ドライバ
  - GPU: amdgpu, nouveau
  - その他: acpitz, thinkpad 等
  - ファン: nct6775/it8688 等のスーパーI/Oチップ

すべて /sys/class/hwmon/hwmon*/temp*_input, fan*_input から読み取る。
macOS/Windows: 外部コマンド不要で空リストを返す (温度は GPU コレクター等で取得)。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


_IS_LINUX = sys.platform.startswith("linux")

# hwmon ドライバ名からカテゴリへのマッピング
_DRIVER_CATEGORY: dict[str, str] = {
    "k10temp": "CPU",
    "coretemp": "CPU",
    "zenpower": "CPU",
    "nvme": "NVMe",
    "drivetemp": "Disk",
    "amdgpu": "GPU",
    "nouveau": "GPU",
    "radeon": "GPU",
    "acpitz": "ACPI",
    "thinkpad": "Thinkpad",
    "iwlwifi_1": "WiFi",
    "nct6775": "Mainboard",
    "nct6776": "Mainboard",
    "nct6779": "Mainboard",
    "nct6791": "Mainboard",
    "nct6792": "Mainboard",
    "nct6793": "Mainboard",
    "nct6795": "Mainboard",
    "nct6796": "Mainboard",
    "nct6798": "Mainboard",
    "it8688": "Mainboard",
    "it8689": "Mainboard",
    "it8665": "Mainboard",
}


@dataclass
class FanSensor:
    """個別のファンセンサー。"""
    label: str               # 表示ラベル ("fan1", "CPU Fan" 等)
    rpm: int                 # 現在回転数 (RPM)
    min_rpm: int = 0         # 最低回転数 (0 = 不明)


@dataclass
class TempSensor:
    """個別の温度センサー。"""
    label: str               # 表示ラベル ("Tctl", "Composite", "Sensor 1" 等)
    temp_c: float            # 現在温度 (℃)
    crit_c: float = 0.0      # クリティカル閾値 (℃, 0 = 不明)
    max_c: float = 0.0       # 最大安全閾値 (℃, 0 = 不明)


@dataclass
class TempDevice:
    """1つの hwmon デバイス (= 1チップ) の温度情報。"""
    name: str                 # ドライバ名 (k10temp, nvme, etc.)
    category: str             # カテゴリ (CPU, NVMe, GPU, etc.)
    device_label: str = ""    # デバイス特定ラベル (nvme0 等)
    sensors: list[TempSensor] = field(default_factory=list)
    fans: list[FanSensor] = field(default_factory=list)

    @property
    def primary_temp_c(self) -> float:
        """代表温度 (最初のセンサーの値)。"""
        return self.sensors[0].temp_c if self.sensors else 0.0

    @property
    def max_temp_c(self) -> float:
        """全センサーの最大温度。"""
        return max((s.temp_c for s in self.sensors), default=0.0)

    @property
    def primary_crit_c(self) -> float:
        """代表のクリティカル温度。"""
        return self.sensors[0].crit_c if self.sensors else 0.0

    @property
    def icon(self) -> str:
        """カテゴリに応じたアイコン。"""
        return {
            "CPU": "⚙",
            "NVMe": "💾",
            "Disk": "💾",
            "GPU": "🎮",
            "ACPI": "🌡",
            "Mainboard": "🔌",
            "WiFi": "📶",
            "Thinkpad": "💻",
        }.get(self.category, "🌡")

    @property
    def display_name(self) -> str:
        """表示用名前。"""
        icon = self.icon
        if self.category == "CPU":
            return f"{icon}CPU ({self.name})"
        if self.device_label and ":" not in self.device_label:
            return f"{icon}{self.category}: {self.device_label}"
        return f"{icon}{self.category}: {self.name}"


def _read_sysfs(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (OSError, PermissionError):
        return ""


def _read_int(path: Path) -> int:
    val = _read_sysfs(path)
    try:
        return int(val)
    except ValueError:
        return 0


class TemperatureCollector:
    """温度・ファンセンサーコレクター。"""

    def __init__(self) -> None:
        self._hwmon_map: dict[str, str] = {}  # hwmonX → device_label

    def _get_device_label(self, hwmon_dir: Path) -> str:
        """hwmon デバイスのラベルを特定。"""
        # device → symlink を辿って PCIe アドレスや NVMe 名を取得
        device_link = hwmon_dir / "device"
        if device_link.is_symlink():
            try:
                real = device_link.resolve()
                name = real.name
                # nvme0, nvme1 など
                if name.startswith("nvme"):
                    return name
                # PCIe アドレス (0000:f1:00.0)
                if ":" in name:
                    return name
            except OSError:
                pass
        return ""

    def collect(self) -> list[TempDevice]:
        if not _IS_LINUX:
            return []

        hwmon_root = Path("/sys/class/hwmon")
        if not hwmon_root.exists():
            return []

        devices: list[TempDevice] = []

        for hwmon_dir in sorted(hwmon_root.iterdir()):
            if not hwmon_dir.is_dir():
                continue

            driver_name = _read_sysfs(hwmon_dir / "name")
            if not driver_name:
                continue

            category = _DRIVER_CATEGORY.get(driver_name, "Other")
            device_label = self._get_device_label(hwmon_dir)

            # temp*_input ファイルを探す
            sensors: list[TempSensor] = []
            for i in range(1, 20):  # temp1 ~ temp19
                temp_file = hwmon_dir / f"temp{i}_input"
                if not temp_file.exists():
                    continue

                millideg = _read_int(temp_file)
                if millideg == 0:
                    continue

                temp_c = millideg / 1000.0

                # ラベル
                label = _read_sysfs(hwmon_dir / f"temp{i}_label")
                if not label:
                    label = f"temp{i}"

                # 閾値
                crit_c = _read_int(hwmon_dir / f"temp{i}_crit") / 1000.0
                max_c = _read_int(hwmon_dir / f"temp{i}_max") / 1000.0

                sensors.append(TempSensor(
                    label=label,
                    temp_c=temp_c,
                    crit_c=crit_c,
                    max_c=max_c,
                ))

            # fan*_input ファイルを探す
            fans: list[FanSensor] = []
            for i in range(1, 10):  # fan1 ~ fan9
                fan_file = hwmon_dir / f"fan{i}_input"
                if not fan_file.exists():
                    continue

                rpm = _read_int(fan_file)
                # RPM 0 は停止中 (表示する)、ファイル自体がない場合はスキップ

                # ラベル
                label = _read_sysfs(hwmon_dir / f"fan{i}_label")
                if not label:
                    label = f"fan{i}"

                # 最低回転数
                min_rpm = _read_int(hwmon_dir / f"fan{i}_min")

                fans.append(FanSensor(
                    label=label,
                    rpm=rpm,
                    min_rpm=min_rpm,
                ))

            if sensors or fans:
                devices.append(TempDevice(
                    name=driver_name,
                    category=category,
                    device_label=device_label,
                    sensors=sensors,
                    fans=fans,
                ))

        return devices
