"""Intel Gaudi (Habana Labs) collector - hl-smi を使用。

hl-smi は nvidia-smi に似たコマンドラインツールで、
Intel Gaudi / Gaudi2 / Gaudi3 アクセラレータの情報を取得できる。

hl-smi -Q index,name,utilization.aip,memory.used,memory.total,temperature.aip,power.draw
       -f csv,noheader,nounits
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class GaudiUsage:
    """1つの Intel Gaudi アクセラレータのメトリクス。"""
    index: int
    name: str = "Unknown"
    aip_util_pct: float = 0.0       # AIP (AI Processor) 使用率
    mem_used_mib: float = 0.0
    mem_total_mib: float = 0.0
    temperature_c: float = 0.0
    power_draw_w: float = 0.0

    @property
    def mem_used_pct(self) -> float:
        return 100.0 * self.mem_used_mib / self.mem_total_mib if self.mem_total_mib else 0.0

    @property
    def short_name(self) -> str:
        n = self.name
        for prefix in ["Intel ", "Habana "]:
            n = n.removeprefix(prefix)
        return n


def _try_hl_smi() -> list[GaudiUsage] | None:
    """hl-smi コマンドで Gaudi メトリクスを取得。"""
    if not shutil.which("hl-smi"):
        return None

    # hl-smi の query 形式
    query_fields = [
        "index", "name", "utilization.aip",
        "memory.used", "memory.total",
        "temperature.aip", "power.draw",
    ]

    try:
        result = subprocess.run(
            ["hl-smi",
             "-Q", ",".join(query_fields),
             "-f", "csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _try_hl_smi_fallback()

    if result.returncode != 0:
        return _try_hl_smi_fallback()

    gpus: list[GaudiUsage] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]

        def _float(s: str) -> float:
            try:
                return float(s)
            except (ValueError, TypeError):
                return 0.0

        idx = int(parts[0]) if parts[0].isdigit() else len(gpus)
        gpus.append(GaudiUsage(
            index=idx,
            name=parts[1] if len(parts) > 1 else "Gaudi",
            aip_util_pct=_float(parts[2]) if len(parts) > 2 else 0.0,
            mem_used_mib=_float(parts[3]) if len(parts) > 3 else 0.0,
            mem_total_mib=_float(parts[4]) if len(parts) > 4 else 0.0,
            temperature_c=_float(parts[5]) if len(parts) > 5 else 0.0,
            power_draw_w=_float(parts[6]) if len(parts) > 6 else 0.0,
        ))

    return gpus if gpus else None


def _try_hl_smi_fallback() -> list[GaudiUsage] | None:
    """hl-smi の通常出力をパースするフォールバック。"""
    if not shutil.which("hl-smi"):
        return None

    try:
        result = subprocess.run(
            ["hl-smi"], capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    # hl-smi のデフォルト出力は nvidia-smi に似たテーブル形式
    # パースは複雑なので、最低限のデバイス検出のみ
    gpus: list[GaudiUsage] = []
    lines = result.stdout.splitlines()
    idx = 0
    for line in lines:
        # "| 0  HL-225   ..." のようなパターンを探す
        stripped = line.strip().strip("|").strip()
        parts = stripped.split()
        if len(parts) >= 2 and parts[0].isdigit():
            gpus.append(GaudiUsage(
                index=int(parts[0]),
                name=" ".join(parts[1:3]) if len(parts) >= 3 else parts[1],
            ))
            idx += 1

    return gpus if gpus else None


class GaudiCollector:
    """Intel Gaudi コレクター。"""

    def available(self) -> bool:
        return bool(shutil.which("hl-smi"))

    def collect(self) -> list[GaudiUsage]:
        return _try_hl_smi() or []
