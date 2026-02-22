"""AMD GPU collector - rocm-smi を使用して AMD GPU (MI300 等) のメトリクスを取得。

rocm-smi の JSON 出力または CSV 出力をパースする。
ROCm がインストールされていない環境では空リストを返す。

対応GPU: MI300X, MI300A, MI250X, MI210, RX 7900 XTX 等 ROCm 対応全般。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class AmdGpuUsage:
    """1つの AMD GPU のメトリクス。"""
    index: int
    name: str = "Unknown"
    gpu_util_pct: float = 0.0
    mem_used_mib: float = 0.0
    mem_total_mib: float = 0.0
    temperature_c: float = 0.0
    power_draw_w: float = 0.0
    power_limit_w: float = 0.0
    fan_speed_pct: float = 0.0

    @property
    def mem_used_pct(self) -> float:
        return 100.0 * self.mem_used_mib / self.mem_total_mib if self.mem_total_mib else 0.0

    @property
    def power_pct(self) -> float:
        return 100.0 * self.power_draw_w / self.power_limit_w if self.power_limit_w else 0.0

    @property
    def short_name(self) -> str:
        n = self.name
        for prefix in ["AMD ", "Advanced Micro Devices "]:
            n = n.removeprefix(prefix)
        return n


def _try_rocm_smi_json() -> list[AmdGpuUsage] | None:
    """rocm-smi --showallinfo --json を試みる。"""
    if not shutil.which("rocm-smi"):
        return None

    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse", "--showmeminfo", "vram",
             "--showtemp", "--showpower", "--showfan", "--json"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return _try_rocm_smi_csv()

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _try_rocm_smi_csv()

    gpus: list[AmdGpuUsage] = []

    # rocm-smi JSON 形式: キーが "card0", "card1", ... の場合
    for i in range(16):
        key = f"card{i}"
        if key not in data:
            continue
        card = data[key]

        def _get(d: dict, *keys: str, default: float = 0.0) -> float:
            for k in keys:
                if k in d:
                    try:
                        return float(str(d[k]).rstrip(" %WC"))
                    except (ValueError, TypeError):
                        pass
            return default

        gpus.append(AmdGpuUsage(
            index=i,
            name=str(card.get("Card series", card.get("card_series", f"AMD GPU {i}"))),
            gpu_util_pct=_get(card, "GPU use (%)", "GPU use", "gpu_use_percent"),
            mem_used_mib=_get(card, "VRAM Total Used Memory (B)", "vram_used") / (1024 * 1024)
                if _get(card, "VRAM Total Used Memory (B)", "vram_used") > 10000
                else _get(card, "VRAM Total Used Memory (B)", "vram_used"),
            mem_total_mib=_get(card, "VRAM Total Memory (B)", "vram_total") / (1024 * 1024)
                if _get(card, "VRAM Total Memory (B)", "vram_total") > 10000
                else _get(card, "VRAM Total Memory (B)", "vram_total"),
            temperature_c=_get(card, "Temperature (Sensor edge) (C)",
                                "temperature_edge", "Temperature"),
            power_draw_w=_get(card, "Average Graphics Package Power (W)",
                              "average_socket_power", "Power"),
            power_limit_w=_get(card, "Max Graphics Package Power (W)",
                               "power_cap"),
            fan_speed_pct=_get(card, "Fan speed (%)", "fan_speed_percent"),
        ))

    return gpus if gpus else None


def _try_rocm_smi_csv() -> list[AmdGpuUsage] | None:
    """rocm-smi の通常出力をパース (フォールバック)。"""
    if not shutil.which("rocm-smi"):
        return None

    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse", "--showmemuse", "--showtemp",
             "--showpower", "--csv"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    lines = result.stdout.strip().splitlines()
    if len(lines) < 2:
        return None

    headers = [h.strip().lower() for h in lines[0].split(",")]
    gpus: list[AmdGpuUsage] = []

    for idx, line in enumerate(lines[1:]):
        vals = [v.strip() for v in line.split(",")]
        row = dict(zip(headers, vals))

        def _f(key: str) -> float:
            v = row.get(key, "0")
            try:
                return float(v.rstrip(" %WC"))
            except (ValueError, TypeError):
                return 0.0

        gpus.append(AmdGpuUsage(
            index=idx,
            name=row.get("device", f"AMD GPU {idx}"),
            gpu_util_pct=_f("gpu use (%)") or _f("gpu_use_%"),
            temperature_c=_f("temperature (sensor edge) (c)") or _f("temp"),
            power_draw_w=_f("average socket power (w)") or _f("power"),
        ))

    return gpus if gpus else None


class AmdGpuCollector:
    """AMD GPU (ROCm) コレクター。"""

    def available(self) -> bool:
        return bool(shutil.which("rocm-smi"))

    def collect(self) -> list[AmdGpuUsage]:
        return _try_rocm_smi_json() or []
