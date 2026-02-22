"""Apple GPU (Metal) collector - ioreg を使用して Apple Silicon GPU メトリクスを取得。

取得戦略:
  1. ioreg -r -d 1 -w 0 -c IOAccelerator -a で PerformanceStatistics を取得
  2. plistlib で構造化パース (正規表現不要)
  3. system_profiler (起動時1回) で GPU 名・コア数を取得

取得するメトリクス:
  - GPU 使用率 (%) — Device Utilization %
  - レンダラー使用率 (%) — Renderer Utilization %
  - タイラー使用率 (%) — Tiler Utilization %
  - GPU メモリ使用量 (MiB) — In use system memory (統合メモリ)
  - GPU メモリ割当量 (MiB) — Alloc system memory

macOS (Apple Silicon M1/M2/M3/M4) 専用。sudo 不要。
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from dataclasses import dataclass

_IS_DARWIN = sys.platform == "darwin"


@dataclass
class AppleGpuUsage:
    """1つの Apple GPU のメトリクス。"""
    index: int
    name: str = "Apple GPU"
    gpu_util_pct: float = 0.0         # Device Utilization %
    renderer_util_pct: float = 0.0    # Renderer Utilization %
    tiler_util_pct: float = 0.0       # Tiler Utilization %
    mem_used_mib: float = 0.0         # In use system memory (MiB)
    mem_alloc_mib: float = 0.0        # Alloc system memory (MiB)
    gpu_core_count: int = 0           # GPU コア数
    metal_family: str = ""            # Metal ファミリ (Metal 3 等)

    @property
    def mem_used_pct(self) -> float:
        return 100.0 * self.mem_used_mib / self.mem_alloc_mib if self.mem_alloc_mib else 0.0

    @property
    def short_name(self) -> str:
        n = self.name
        for prefix in ["Apple "]:
            n = n.removeprefix(prefix)
        return n


def _get_static_info() -> tuple[str, int, str]:
    """system_profiler から GPU 名・コア数・Metal ファミリを取得 (起動時1回)。"""
    name = "Apple GPU"
    cores = 0
    metal = ""
    try:
        out = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            data = json.loads(out.stdout)
            displays = data.get("SPDisplaysDataType", [])
            if displays:
                d = displays[0]
                name = d.get("sppci_model", d.get("_name", name))
                cores_str = d.get("sppci_cores", "0")
                try:
                    cores = int(cores_str)
                except (ValueError, TypeError):
                    pass
                metal_raw = d.get("spdisplays_mtlgpufamilysupport", "")
                if metal_raw:
                    metal = metal_raw.replace("spdisplays_", "").replace("metal", "Metal ").strip()
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return name, cores, metal


def _collect_ioreg() -> list[AppleGpuUsage] | None:
    """ioreg -c IOAccelerator -a から GPU メトリクスを取得。"""
    try:
        result = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator", "-a"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0 or not result.stdout:
        return None

    try:
        entries = plistlib.loads(result.stdout)
    except (plistlib.InvalidFileException, Exception):
        return None

    if not isinstance(entries, list) or not entries:
        return None

    gpus: list[AppleGpuUsage] = []
    for i, entry in enumerate(entries):
        ps = entry.get("PerformanceStatistics", {})
        if not ps:
            continue

        gpu_util = ps.get("Device Utilization %", 0)
        renderer_util = ps.get("Renderer Utilization %", 0)
        tiler_util = ps.get("Tiler Utilization %", 0)

        in_use_bytes = ps.get("In use system memory", 0)
        alloc_bytes = ps.get("Alloc system memory", 0)

        in_use_mib = in_use_bytes / (1024 * 1024)
        alloc_mib = alloc_bytes / (1024 * 1024)

        # ioreg 上の GPU 名
        model = entry.get("model", "")
        core_count = entry.get("gpu-core-count", 0)

        gpus.append(AppleGpuUsage(
            index=i,
            name=model or "Apple GPU",
            gpu_util_pct=float(gpu_util),
            renderer_util_pct=float(renderer_util),
            tiler_util_pct=float(tiler_util),
            mem_used_mib=in_use_mib,
            mem_alloc_mib=alloc_mib,
            gpu_core_count=core_count,
        ))

    return gpus if gpus else None


class AppleGpuCollector:
    """Apple Silicon GPU (Metal) コレクター。

    macOS + Apple Silicon 環境でのみ動作。sudo 不要。
    ioreg で PerformanceStatistics を取得し、
    起動時に system_profiler で静的情報をキャッシュする。
    """

    def __init__(self) -> None:
        self._static_name: str = ""
        self._static_cores: int = 0
        self._static_metal: str = ""
        self._static_loaded: bool = False

    def _ensure_static(self) -> None:
        if not self._static_loaded:
            self._static_name, self._static_cores, self._static_metal = _get_static_info()
            self._static_loaded = True

    @staticmethod
    def available() -> bool:
        """Apple Silicon GPU が利用可能か。"""
        if not _IS_DARWIN:
            return False
        try:
            result = subprocess.run(
                ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator", "-a"],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout:
                return False
            entries = plistlib.loads(result.stdout)
            return bool(entries) and any(
                e.get("PerformanceStatistics") for e in entries
            )
        except (OSError, subprocess.TimeoutExpired, Exception):
            return False

    def collect(self) -> list[AppleGpuUsage]:
        if not _IS_DARWIN:
            return []

        self._ensure_static()
        gpus = _collect_ioreg()
        if not gpus:
            return []

        # 静的情報をマージ
        for g in gpus:
            if self._static_name and (not g.name or g.name == "Apple GPU"):
                g.name = self._static_name
            if self._static_cores and not g.gpu_core_count:
                g.gpu_core_count = self._static_cores
            if self._static_metal:
                g.metal_family = self._static_metal

        return gpus
