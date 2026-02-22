"""Memory and Swap collector - reads /proc/meminfo.

/proc/meminfo から主要なメモリ情報を読み取る:
  MemTotal, MemFree, MemAvailable, Buffers, Cached,
  SwapTotal, SwapFree, SwapCached など
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MemoryUsage:
    """メモリ使用状況 (KB 単位)。"""
    total_kb: int = 0
    used_kb: int = 0        # アプリケーションが実際に使用中
    buffers_kb: int = 0     # カーネルバッファ
    cached_kb: int = 0      # ページキャッシュ
    free_kb: int = 0        # 完全に未使用

    @property
    def used_pct(self) -> float:
        return 100.0 * self.used_kb / self.total_kb if self.total_kb else 0.0

    @property
    def buffers_pct(self) -> float:
        return 100.0 * self.buffers_kb / self.total_kb if self.total_kb else 0.0

    @property
    def cached_pct(self) -> float:
        return 100.0 * self.cached_kb / self.total_kb if self.total_kb else 0.0

    @property
    def free_pct(self) -> float:
        return 100.0 * self.free_kb / self.total_kb if self.total_kb else 0.0


@dataclass
class SwapUsage:
    """スワップ使用状況 (KB 単位)。"""
    total_kb: int = 0
    used_kb: int = 0
    cached_kb: int = 0
    free_kb: int = 0

    @property
    def used_pct(self) -> float:
        return 100.0 * self.used_kb / self.total_kb if self.total_kb else 0.0

    @property
    def free_pct(self) -> float:
        return 100.0 * self.free_kb / self.total_kb if self.total_kb else 0.0


class MemoryCollector:
    """Memory / Swap コレクター。"""

    @staticmethod
    def _read_meminfo() -> dict[str, int]:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                key = parts[0].rstrip(":")
                info[key] = int(parts[1])  # KB
        return info

    def collect(self) -> tuple[MemoryUsage, SwapUsage]:
        m = self._read_meminfo()

        total = m.get("MemTotal", 0)
        free = m.get("MemFree", 0)
        buffers = m.get("Buffers", 0)
        cached = m.get("Cached", 0) + m.get("SReclaimable", 0)
        used = total - free - buffers - cached

        mem = MemoryUsage(
            total_kb=total,
            used_kb=max(used, 0),
            buffers_kb=buffers,
            cached_kb=cached,
            free_kb=free,
        )

        swap_total = m.get("SwapTotal", 0)
        swap_free = m.get("SwapFree", 0)
        swap_cached = m.get("SwapCached", 0)
        swap_used = swap_total - swap_free - swap_cached

        swap = SwapUsage(
            total_kb=swap_total,
            used_kb=max(swap_used, 0),
            cached_kb=swap_cached,
            free_kb=swap_free,
        )

        return mem, swap
