"""NVIDIA GPU collector - nvidia-smi を使用して GPU メトリクスを取得。

取得戦略:
  1. pynvml (Python NVML バインディング) が利用可能ならそれを使用
  2. フォールバックとして nvidia-smi コマンドの CSV 出力をパース
  3. macOS: system_profiler で GPU 名/VRAM を取得
  4. Windows: WMI Win32_VideoController で非NVIDIA GPU の情報を取得

取得するメトリクス:
  - GPU 使用率 (%)
  - VRAM 使用量 / 総量 (MiB)
  - 温度 (°C)
  - 消費電力 (W) / 電力上限 (W)
  - ファン速度 (%)
  - エンコーダ/デコーダ使用率 (%)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field

_IS_DARWIN = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"


@dataclass
class GpuUsage:
    """1つの GPU のメトリクス。"""
    index: int
    name: str = "Unknown"
    gpu_util_pct: float = 0.0       # GPU コア使用率
    mem_used_mib: float = 0.0       # VRAM 使用量
    mem_total_mib: float = 0.0      # VRAM 総量
    temperature_c: float = 0.0      # 温度
    temp_slowdown_c: float = 0.0    # スロットリング温度
    temp_shutdown_c: float = 0.0    # シャットダウン温度
    temp_max_c: float = 0.0         # 最大動作温度
    power_draw_w: float = 0.0       # 現在の消費電力
    power_limit_w: float = 0.0      # 電力上限
    fan_speed_pct: float = 0.0      # ファン速度
    encoder_util_pct: float = 0.0   # エンコーダ使用率
    decoder_util_pct: float = 0.0   # デコーダ使用率

    @property
    def mem_used_pct(self) -> float:
        return 100.0 * self.mem_used_mib / self.mem_total_mib if self.mem_total_mib else 0.0

    @property
    def power_pct(self) -> float:
        return 100.0 * self.power_draw_w / self.power_limit_w if self.power_limit_w else 0.0

    @property
    def short_name(self) -> str:
        # "NVIDIA RTX PRO 6000 ..." -> "RTX PRO 6000"
        n = self.name.replace("NVIDIA ", "")
        # さらに長い suffix を切る
        for suffix in ["Workstation Edition", "Laptop GPU", "Max-Q"]:
            n = n.replace(suffix, "").strip()
        return n


def _try_nvml() -> list[GpuUsage] | None:
    """pynvml が利用可能か確認だけ行う (初回チェック用)。"""
    try:
        import pynvml  # type: ignore[import-untyped]
        pynvml.nvmlInit()
        pynvml.nvmlShutdown()
        return []  # 利用可能を示す (空リスト)
    except (ImportError, Exception):
        return None


def _try_nvidia_smi() -> list[GpuUsage] | None:
    """nvidia-smi の CSV 出力をパースして収集。"""
    if not shutil.which("nvidia-smi"):
        return None

    query = ",".join([
        "index",
        "name",
        "utilization.gpu",
        "memory.used",
        "memory.total",
        "temperature.gpu",
        "power.draw",
        "power.limit",
        "fan.speed",
        "encoder.stats.sessionCount",  # encoder は近似
        "decoder.stats.sessionCount",
    ])

    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        # フォールバック: シンプルなクエリ
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,"
                    "temperature.gpu,power.draw,power.limit",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

        if result.returncode != 0:
            return None

    gpus: list[GpuUsage] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]

        def _float(s: str) -> float:
            try:
                return float(s)
            except (ValueError, TypeError):
                return 0.0

        idx = int(parts[0]) if parts[0].isdigit() else len(gpus)
        gpus.append(GpuUsage(
            index=idx,
            name=parts[1] if len(parts) > 1 else "Unknown",
            gpu_util_pct=_float(parts[2]) if len(parts) > 2 else 0.0,
            mem_used_mib=_float(parts[3]) if len(parts) > 3 else 0.0,
            mem_total_mib=_float(parts[4]) if len(parts) > 4 else 0.0,
            temperature_c=_float(parts[5]) if len(parts) > 5 else 0.0,
            power_draw_w=_float(parts[6]) if len(parts) > 6 else 0.0,
            power_limit_w=_float(parts[7]) if len(parts) > 7 else 0.0,
            fan_speed_pct=_float(parts[8]) if len(parts) > 8 else 0.0,
        ))

    return gpus if gpus else None


class GpuCollector:
    """NVIDIA GPU コレクター。

    pynvml > nvidia-smi の順にフォールバックする。
    GPU が見つからない場合は空リストを返す。
    pynvml はハンドルを永続的にキャッシュして Init/Shutdown のオーバーヘッドを回避。
    """

    def __init__(self) -> None:
        self._use_nvml: bool | None = None  # None = 未判定
        self._nvml_ok = False
        self._handles: list = []
        self._names: list[str] = []
        self._power_limits: list[float] = []
        self._temp_thresholds: list[tuple[float, float, float]] = []  # (max, slowdown, shutdown)
        self._pynvml = None  # モジュール参照

    def available(self) -> bool:
        """GPU モニタリングが利用可能か。"""
        return bool(shutil.which("nvidia-smi"))

    def _init_nvml(self) -> bool:
        """pynvml を初期化してハンドルをキャッシュ。"""
        try:
            import pynvml  # type: ignore[import-untyped]
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            handles = []
            names = []
            power_limits = []
            temp_thresholds = []
            for i in range(count):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                handles.append(h)
                n = pynvml.nvmlDeviceGetName(h)
                if isinstance(n, bytes):
                    n = n.decode()
                names.append(n)
                try:
                    pl = pynvml.nvmlDeviceGetPowerManagementLimit(h) / 1000.0
                except pynvml.NVMLError:
                    pl = 0.0
                power_limits.append(pl)
                # 温度閾値取得
                t_max = t_slow = t_shut = 0.0
                try:
                    t_max = float(pynvml.nvmlDeviceGetTemperatureThreshold(
                        h, pynvml.NVML_TEMPERATURE_THRESHOLD_GPU_MAX))
                except (pynvml.NVMLError, Exception):
                    pass
                try:
                    t_slow = float(pynvml.nvmlDeviceGetTemperatureThreshold(
                        h, pynvml.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN))
                except (pynvml.NVMLError, Exception):
                    pass
                try:
                    t_shut = float(pynvml.nvmlDeviceGetTemperatureThreshold(
                        h, pynvml.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN))
                except (pynvml.NVMLError, Exception):
                    pass
                temp_thresholds.append((t_max, t_slow, t_shut))
            self._pynvml = pynvml
            self._handles = handles
            self._names = names
            self._power_limits = power_limits
            self._temp_thresholds = temp_thresholds
            self._nvml_ok = True
            self._use_nvml = True
            return True
        except Exception:
            self._use_nvml = False
            self._nvml_ok = False
            return False

    def _collect_nvml(self) -> list[GpuUsage]:
        """キャッシュ済みハンドルで高速に収集。"""
        pynvml = self._pynvml
        gpus: list[GpuUsage] = []
        try:
            for i, handle in enumerate(self._handles):
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)

                try:
                    temp = pynvml.nvmlDeviceGetTemperature(
                        handle, pynvml.NVML_TEMPERATURE_GPU)
                except pynvml.NVMLError:
                    temp = 0
                try:
                    power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
                except pynvml.NVMLError:
                    power = 0.0
                try:
                    fan = pynvml.nvmlDeviceGetFanSpeed(handle)
                except pynvml.NVMLError:
                    fan = 0
                try:
                    enc_util, _ = pynvml.nvmlDeviceGetEncoderUtilization(handle)
                except pynvml.NVMLError:
                    enc_util = 0
                try:
                    dec_util, _ = pynvml.nvmlDeviceGetDecoderUtilization(handle)
                except pynvml.NVMLError:
                    dec_util = 0

                t_max, t_slow, t_shut = (
                    self._temp_thresholds[i]
                    if i < len(self._temp_thresholds) else (0.0, 0.0, 0.0))
                gpus.append(GpuUsage(
                    index=i,
                    name=self._names[i],
                    gpu_util_pct=float(util.gpu),
                    mem_used_mib=mem_info.used / (1024 * 1024),
                    mem_total_mib=mem_info.total / (1024 * 1024),
                    temperature_c=float(temp),
                    temp_slowdown_c=t_slow,
                    temp_shutdown_c=t_shut,
                    temp_max_c=t_max,
                    power_draw_w=power,
                    power_limit_w=self._power_limits[i],
                    fan_speed_pct=float(fan),
                    encoder_util_pct=float(enc_util),
                    decoder_util_pct=float(dec_util),
                ))
            return gpus
        except Exception:
            # NVML がクラッシュしたらフォールバック
            self._nvml_ok = False
            self._use_nvml = False
            return _try_nvidia_smi() or []

    def collect(self) -> list[GpuUsage]:
        if self._use_nvml is None:
            self._init_nvml()

        if self._use_nvml and self._nvml_ok:
            return self._collect_nvml()

        result = _try_nvidia_smi()
        if result:
            return result

        # NVIDIA 以外: macOS/Windows で基本 GPU 情報を取得
        if _IS_DARWIN:
            return _try_macos_gpu()
        if _IS_WIN:
            return _try_win_gpu()

        return []


def _try_macos_gpu() -> list[GpuUsage]:
    """macOS: system_profiler で GPU 名と VRAM を取得。"""
    try:
        import json as _json
        out = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return []
        data = _json.loads(out.stdout)
        displays = data.get("SPDisplaysDataType", [])
        gpus: list[GpuUsage] = []
        for i, d in enumerate(displays):
            name = d.get("sppci_model", d.get("_name", "Unknown"))
            vram_str = d.get("spdisplays_vram", "0")
            # "1536 MB" or "16 GB"
            vram_mib = 0.0
            try:
                parts = vram_str.split()
                val = float(parts[0])
                if len(parts) > 1 and parts[1].upper().startswith("G"):
                    vram_mib = val * 1024
                else:
                    vram_mib = val
            except (ValueError, IndexError):
                pass
            gpus.append(GpuUsage(
                index=i, name=name,
                mem_total_mib=vram_mib,
            ))
        return gpus
    except (OSError, subprocess.TimeoutExpired, Exception):
        return []


def _try_win_gpu() -> list[GpuUsage]:
    """Windows: WMI Win32_VideoController で非NVIDIA GPU 情報を取得。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController"
             " | ForEach-Object { $_.Name + '|' + $_.AdapterRAM }"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return []
        gpus: list[GpuUsage] = []
        for i, line in enumerate(out.stdout.strip().splitlines()):
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.split("|")
            name = parts[0].strip()
            try:
                adapter_ram = int(parts[1].strip())
                vram_mib = adapter_ram / (1024 * 1024)
            except (ValueError, IndexError):
                vram_mib = 0.0
            # Skip Microsoft Basic Display Adapter
            if "basic" in name.lower():
                continue
            gpus.append(GpuUsage(
                index=i, name=name,
                mem_total_mib=vram_mib,
            ))
        return gpus
    except (OSError, subprocess.TimeoutExpired):
        return []
