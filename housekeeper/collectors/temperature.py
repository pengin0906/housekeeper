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

import shutil
import subprocess
import sys
import threading
import time
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
    def primary_max_c(self) -> float:
        """代表の警告温度 (temp_max)。"""
        return self.sensors[0].max_c if self.sensors else 0.0

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
            "VRM": "⚡",
            "DDR": "🧩",
            "WiFi": "📶",
            "Thinkpad": "💻",
        }.get(self.category, "🌡")

    @property
    def display_name(self) -> str:
        """表示用名前。"""
        icon = self.icon
        if self.category == "CPU":
            return f"{icon}CPU ({self.name})"
        if self.name == "ipmi":
            return f"{icon}{self.category}"
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


def _fast_read(path: str) -> str:
    """raw open() でファイルを高速に読む。"""
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, PermissionError):
        return ""


def _fast_read_int(path: str) -> int:
    """raw open() で整数値を高速に読む。"""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, PermissionError, ValueError):
        return 0


@dataclass
class _CachedTempSensor:
    """キャッシュ済みの温度センサーパス情報。"""
    input_path: str        # temp*_input のフルパス
    label: str             # 事前解決済みラベル
    crit_path: str         # temp*_crit のフルパス ("" なら読まない)
    max_path: str          # temp*_max のフルパス ("" なら読まない)
    crit_c: float          # キャッシュした閾値 (変動しない)
    max_c: float           # キャッシュした閾値 (変動しない)


@dataclass
class _CachedFanSensor:
    """キャッシュ済みのファンセンサーパス情報。"""
    input_path: str        # fan*_input のフルパス
    label: str             # 事前解決済みラベル
    min_rpm: int           # キャッシュした最低回転数


@dataclass
class _CachedHwmon:
    """キャッシュ済みの hwmon デバイス情報。"""
    driver_name: str
    category: str
    device_label: str
    temp_sensors: list[_CachedTempSensor]
    fan_sensors: list[_CachedFanSensor]


class TemperatureCollector:
    """温度・ファンセンサーコレクター。

    初回にセンサーレイアウトを発見してキャッシュし、
    2回目以降は既知パスのみ読み取ることでファイルI/Oを大幅に削減。
    IPMI (ipmitool) 対応: MB温度・ファン・DDR温度等を取得。
    """

    def __init__(self) -> None:
        self._layout: list[_CachedHwmon] | None = None
        self._layout_tick = 0  # 30回ごとにレイアウト再発見
        self._cache: list[TempDevice] | None = None
        self._cache_time: float = 0.0
        # IPMI (非同期)
        self._has_ipmi: bool | None = None  # None=未チェック
        self._ipmi_cache: list[TempDevice] = []
        self._ipmi_cache_time: float = 0.0
        self._ipmi_thread: threading.Thread | None = None
        self._ipmi_pending: list[TempDevice] | None = None  # スレッド結果

    @staticmethod
    def _get_device_label(hwmon_dir: str) -> str:
        """hwmon デバイスのラベルを特定。"""
        import os
        device_link = os.path.join(hwmon_dir, "device")
        try:
            if os.path.islink(device_link):
                real = os.path.realpath(device_link)
                name = os.path.basename(real)
                if name.startswith("nvme") or ":" in name:
                    return name
        except OSError:
            pass
        return ""

    def _discover_layout(self) -> list[_CachedHwmon]:
        """全 hwmon デバイスをスキャンしてセンサーパスをキャッシュ。"""
        import os
        hwmon_root = "/sys/class/hwmon"
        if not os.path.isdir(hwmon_root):
            return []

        result: list[_CachedHwmon] = []
        try:
            entries = sorted(os.listdir(hwmon_root))
        except OSError:
            return []

        for entry in entries:
            hwmon_dir = os.path.join(hwmon_root, entry)
            if not os.path.isdir(hwmon_dir):
                continue

            driver_name = _fast_read(os.path.join(hwmon_dir, "name"))
            if not driver_name:
                continue

            category = _DRIVER_CATEGORY.get(driver_name, "Other")
            device_label = self._get_device_label(hwmon_dir)

            # 温度センサーを発見
            temp_sensors: list[_CachedTempSensor] = []
            for i in range(1, 20):
                input_path = os.path.join(hwmon_dir, f"temp{i}_input")
                if not os.path.exists(input_path):
                    continue

                # ラベルは不変なのでキャッシュ
                label = _fast_read(os.path.join(hwmon_dir, f"temp{i}_label"))
                if not label:
                    label = f"temp{i}"

                # 閾値も不変なのでキャッシュ
                crit_c = _fast_read_int(os.path.join(hwmon_dir, f"temp{i}_crit")) / 1000.0
                max_c = _fast_read_int(os.path.join(hwmon_dir, f"temp{i}_max")) / 1000.0

                temp_sensors.append(_CachedTempSensor(
                    input_path=input_path,
                    label=label,
                    crit_path="",  # 閾値はキャッシュ済み
                    max_path="",
                    crit_c=crit_c,
                    max_c=max_c,
                ))

            # ファンセンサーを発見
            fan_sensors: list[_CachedFanSensor] = []
            for i in range(1, 10):
                input_path = os.path.join(hwmon_dir, f"fan{i}_input")
                if not os.path.exists(input_path):
                    continue

                label = _fast_read(os.path.join(hwmon_dir, f"fan{i}_label"))
                if not label:
                    label = f"fan{i}"
                min_rpm = _fast_read_int(os.path.join(hwmon_dir, f"fan{i}_min"))

                fan_sensors.append(_CachedFanSensor(
                    input_path=input_path,
                    label=label,
                    min_rpm=min_rpm,
                ))

            if temp_sensors or fan_sensors:
                result.append(_CachedHwmon(
                    driver_name=driver_name,
                    category=category,
                    device_label=device_label,
                    temp_sensors=temp_sensors,
                    fan_sensors=fan_sensors,
                ))

        return result

    def collect(self) -> list[TempDevice]:
        if not _IS_LINUX:
            return []

        # hwmon 読み取りは物理的に遅い (~3ms/sensor) ため、5秒間キャッシュ
        now = time.monotonic()
        if self._cache is not None and now - self._cache_time < 5.0:
            return self._cache

        # レイアウト発見 (初回 + 30サイクルごとに再発見)
        if self._layout is None or self._layout_tick >= 30:
            self._layout = self._discover_layout()
            self._layout_tick = 0
        self._layout_tick += 1

        # キャッシュ済みパスのみ読み取り (高速)
        devices: list[TempDevice] = []
        for hw in self._layout:
            sensors: list[TempSensor] = []
            for ts in hw.temp_sensors:
                millideg = _fast_read_int(ts.input_path)
                if millideg == 0:
                    continue
                sensors.append(TempSensor(
                    label=ts.label,
                    temp_c=millideg / 1000.0,
                    crit_c=ts.crit_c,
                    max_c=ts.max_c,
                ))

            fans: list[FanSensor] = []
            for fs in hw.fan_sensors:
                rpm = _fast_read_int(fs.input_path)
                fans.append(FanSensor(
                    label=fs.label,
                    rpm=rpm,
                    min_rpm=fs.min_rpm,
                ))

            if sensors or fans:
                devices.append(TempDevice(
                    name=hw.driver_name,
                    category=hw.category,
                    device_label=hw.device_label,
                    sensors=sensors,
                    fans=fans,
                ))

        # IPMI データを追加
        ipmi_devs = self._collect_ipmi(now)
        if ipmi_devs:
            devices.extend(ipmi_devs)

        self._cache = devices
        self._cache_time = now
        return devices

    # ─── IPMI (ipmitool) ────────────────────────────────────

    def _collect_ipmi(self, now: float) -> list[TempDevice]:
        """ipmitool sdr から MB 温度・ファン・DDR 温度等を取得。

        バックグラウンドスレッドで非同期実行し、GUIをブロックしない。
        10 秒間キャッシュ。
        """
        # ipmitool の存在チェック (初回のみ)
        if self._has_ipmi is None:
            self._has_ipmi = bool(shutil.which("ipmitool"))
            self._ipmi_cmd: list[str] = []
        if not self._has_ipmi:
            return []

        # バックグラウンドスレッドの結果を回収
        if self._ipmi_pending is not None:
            self._ipmi_cache = self._ipmi_pending
            self._ipmi_cache_time = now
            self._ipmi_pending = None

        # キャッシュ有効なら返す
        if self._ipmi_cache and now - self._ipmi_cache_time < 10.0:
            return self._ipmi_cache

        # バックグラウンドスレッドが走っていなければ起動
        if self._ipmi_thread is None or not self._ipmi_thread.is_alive():
            self._ipmi_thread = threading.Thread(
                target=self._ipmi_worker, daemon=True)
            self._ipmi_thread.start()

        return self._ipmi_cache

    def _ipmi_worker(self) -> None:
        """バックグラウンドで ipmitool を実行。"""
        # 初回: 動作するコマンドを探す
        if not self._ipmi_cmd:
            for cmd in [["ipmitool"], ["sudo", "-n", "ipmitool"]]:
                try:
                    r = subprocess.run(
                        cmd + ["sdr", "list"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        self._ipmi_cmd = cmd
                        result = r
                        break
                except (subprocess.TimeoutExpired, FileNotFoundError,
                        PermissionError):
                    continue
            else:
                self._has_ipmi = False
                return
        else:
            try:
                result = subprocess.run(
                    self._ipmi_cmd + ["sdr", "list"],
                    capture_output=True, text=True, timeout=5,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError,
                    PermissionError):
                return

            if result.returncode != 0:
                return

        # パース
        mb_temps: list[TempSensor] = []
        mb_fans: list[FanSensor] = []
        ddr_temps: list[TempSensor] = []
        vrm_temps: list[TempSensor] = []
        other_temps: list[TempSensor] = []

        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            name = parts[0]
            value = parts[1]
            status = parts[2]

            if status not in ("ok",):
                continue  # "ns", "Not Readable" 等はスキップ

            # ファン
            if "FAN" in name.upper() and "RPM" in value:
                try:
                    rpm = int(value.replace("RPM", "").strip())
                    mb_fans.append(FanSensor(label=name, rpm=rpm))
                except ValueError:
                    pass
                continue

            # 温度
            if "degrees C" in value:
                try:
                    temp_c = float(value.replace("degrees C", "").strip())
                except ValueError:
                    continue

                name_upper = name.upper()
                sensor = TempSensor(label=name, temp_c=temp_c)

                if "DDR" in name_upper:
                    ddr_temps.append(sensor)
                elif "VRM" in name_upper:
                    vrm_temps.append(sensor)
                elif "MB" in name_upper or name_upper == "TEMP_CPU":
                    mb_temps.append(sensor)
                elif "LAN" in name_upper:
                    other_temps.append(sensor)
                else:
                    other_temps.append(sensor)

        devices: list[TempDevice] = []

        if mb_temps or mb_fans:
            devices.append(TempDevice(
                name="ipmi",
                category="Mainboard",
                device_label="IPMI BMC",
                sensors=mb_temps,
                fans=mb_fans,
            ))

        if vrm_temps:
            devices.append(TempDevice(
                name="ipmi",
                category="VRM",
                device_label="VRM",
                sensors=vrm_temps,
            ))

        if ddr_temps:
            devices.append(TempDevice(
                name="ipmi",
                category="DDR",
                device_label="DDR5",
                sensors=ddr_temps,
            ))

        if other_temps:
            devices.append(TempDevice(
                name="ipmi",
                category="Other",
                device_label="IPMI",
                sensors=other_temps,
            ))

        # メインスレッドに結果を渡す (次の collect() で回収)
        self._ipmi_pending = devices
