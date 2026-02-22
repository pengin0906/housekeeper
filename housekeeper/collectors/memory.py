"""Memory and Swap collector.

Linux: /proc/meminfo を読み取る。
macOS: sysctl hw.memsize + vm_stat + sysctl vm.swapusage で取得。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass
class MemoryUsage:
    """メモリ使用状況 (KB 単位)。"""
    total_kb: int = 0
    used_kb: int = 0
    buffers_kb: int = 0
    cached_kb: int = 0
    free_kb: int = 0

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


_IS_DARWIN = sys.platform == "darwin"


class MemoryCollector:
    """Memory / Swap コレクター。"""

    def _read_meminfo(self) -> dict[str, int]:
        if _IS_DARWIN:
            return self._read_meminfo_darwin()
        return self._read_meminfo_linux()

    @staticmethod
    def _read_meminfo_linux() -> dict[str, int]:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                key = parts[0].rstrip(":")
                info[key] = int(parts[1])  # KB
        return info

    @staticmethod
    def _read_meminfo_darwin() -> dict[str, int]:
        """macOS: sysctl + vm_stat でメモリ情報を取得。"""
        info: dict[str, int] = {}

        # 物理メモリ合計
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0:
                info["MemTotal"] = int(out.stdout.strip()) // 1024  # bytes→KB
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

        # vm_stat でページ統計
        try:
            out = subprocess.run(
                ["vm_stat"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0:
                page_size = 16384  # default
                stats: dict[str, int] = {}
                for line in out.stdout.splitlines():
                    if "page size of" in line:
                        try:
                            page_size = int(line.split()[-2])
                        except (ValueError, IndexError):
                            pass
                        continue
                    if ":" in line:
                        key, val = line.split(":", 1)
                        key = key.strip()
                        val = val.strip().rstrip(".")
                        try:
                            stats[key] = int(val)
                        except ValueError:
                            pass

                pg_kb = page_size // 1024
                free_pages = stats.get("Pages free", 0)
                active = stats.get("Pages active", 0)
                inactive = stats.get("Pages inactive", 0)
                speculative = stats.get("Pages speculative", 0)
                wired = stats.get("Pages wired down", 0)
                purgeable = stats.get("Pages purgeable", 0)

                info["MemFree"] = free_pages * pg_kb
                info["Cached"] = (inactive + purgeable + speculative) * pg_kb
                info["Buffers"] = 0
                # used = total - free - cached
        except (OSError, subprocess.TimeoutExpired):
            pass

        # スワップ
        try:
            out = subprocess.run(
                ["sysctl", "-n", "vm.swapusage"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0:
                # "total = 2048.00M  used = 100.00M  free = 1948.00M  ..."
                for part in out.stdout.split():
                    pass  # parse below
                text = out.stdout.strip()
                swap_total = swap_used = swap_free = 0
                for seg in text.split("  "):
                    seg = seg.strip()
                    if seg.startswith("total"):
                        val = seg.split("=")[1].strip().rstrip("M")
                        swap_total = int(float(val) * 1024)
                    elif seg.startswith("used"):
                        val = seg.split("=")[1].strip().rstrip("M")
                        swap_used = int(float(val) * 1024)
                    elif seg.startswith("free"):
                        val = seg.split("=")[1].strip().rstrip("M")
                        swap_free = int(float(val) * 1024)
                info["SwapTotal"] = swap_total
                info["SwapFree"] = swap_free
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

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
