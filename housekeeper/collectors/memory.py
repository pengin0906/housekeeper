"""Memory and Swap collector.

Linux: /proc/meminfo を読み取る。
macOS: sysctl hw.memsize + vm_stat + sysctl vm.swapusage で取得。
メモリ帯域: Linux resctrl MBM (AMD QoS / Intel RDT) で実測。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MemoryUsage:
    """メモリ使用状況 (KB 単位)。"""
    total_kb: int = 0
    used_kb: int = 0
    buffers_kb: int = 0
    cached_kb: int = 0
    free_kb: int = 0
    bw_gbs: float = 0.0      # メモリ帯域 合計 (GB/s) - resctrl MBM
    bw_read_gbs: float = 0.0  # メモリ帯域 Read (GB/s)
    bw_write_gbs: float = 0.0 # メモリ帯域 Write (GB/s)

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
_IS_WIN = sys.platform == "win32"


_RESCTRL_MON = Path("/sys/fs/resctrl/mon_data")
_RESCTRL_INFO = Path("/sys/fs/resctrl/info/L3_MON")

# AMD QoS mbm_*_bytes_config bitmask:
#   bit0: total reads, bit1: total writes, bit2: slow reads, bit3: slow writes,
#   bit4: NT IO writes, bit5: NT IO reads, bit6: dirty victims (writebacks)
_MBM_READ_MASK = 0x25   # bit0 + bit2 + bit5 = all reads
_MBM_WRITE_MASK = 0x5a  # bit1 + bit3 + bit4 + bit6 = all writes


class MemoryCollector:
    """Memory / Swap コレクター。"""

    def __init__(self) -> None:
        # resctrl MBM 帯域計測用
        self._mbm_total_files: list[Path] = []  # mbm_total_bytes (read or total)
        self._mbm_local_files: list[Path] = []  # mbm_local_bytes (write, if split)
        self._mbm_rw_split: bool = False
        self._mbm_prev_total: int = 0
        self._mbm_prev_local: int = 0
        self._mbm_prev_time: float = 0.0
        if not _IS_DARWIN and not _IS_WIN and _RESCTRL_MON.is_dir():
            for d in sorted(_RESCTRL_MON.iterdir()):
                ft = d / "mbm_total_bytes"
                fl = d / "mbm_local_bytes"
                if ft.exists():
                    self._mbm_total_files.append(ft)
                if fl.exists():
                    self._mbm_local_files.append(fl)
            # R/W 分離: まず既存設定を確認、なければ設定試行
            if self._mbm_total_files and self._mbm_local_files:
                self._mbm_rw_split = self._detect_rw_split()
                if not self._mbm_rw_split:
                    self._mbm_rw_split = self._try_configure_rw_split()

    @staticmethod
    def _detect_rw_split() -> bool:
        """既に R/W 分離設定がされているか確認。"""
        tc = _RESCTRL_INFO / "mbm_total_bytes_config"
        lc = _RESCTRL_INFO / "mbm_local_bytes_config"
        try:
            t_val = int(tc.read_text().strip().split(";")[0].split("=")[1], 16)
            l_val = int(lc.read_text().strip().split(";")[0].split("=")[1], 16)
            # total が read マスク、local が write マスクなら分離済み
            return t_val == _MBM_READ_MASK and l_val == _MBM_WRITE_MASK
        except (OSError, ValueError, IndexError):
            return False

    @staticmethod
    def _try_configure_rw_split() -> bool:
        """mbm_total→Read, mbm_local→Write に設定変更を試行。"""
        tc = _RESCTRL_INFO / "mbm_total_bytes_config"
        lc = _RESCTRL_INFO / "mbm_local_bytes_config"
        if not tc.exists() or not lc.exists():
            return False
        try:
            # 現在の設定を読み取り: "0=0x7f;1=0x7f" 形式
            cur = tc.read_text().strip()
            domains = [s.split("=")[0] for s in cur.split(";")]
            # sysfs は Python write_text() で書けない場合があるため shell 経由
            read_cfg = ";".join(f"{d}={_MBM_READ_MASK:#x}" for d in domains)
            write_cfg = ";".join(f"{d}={_MBM_WRITE_MASK:#x}" for d in domains)
            r1 = subprocess.run(
                f"echo '{read_cfg}' > {tc}",
                shell=True, capture_output=True, timeout=2)
            r2 = subprocess.run(
                f"echo '{write_cfg}' > {lc}",
                shell=True, capture_output=True, timeout=2)
            if r1.returncode != 0 or r2.returncode != 0:
                return False
            # 検証
            actual = tc.read_text().strip()
            return _MBM_READ_MASK == int(actual.split(";")[0].split("=")[1], 16)
        except (OSError, PermissionError, subprocess.TimeoutExpired, ValueError):
            return False

    def _read_mbm_bandwidth(self) -> tuple[float, float, float]:
        """resctrl MBM からメモリ帯域 (GB/s) を計算。

        Returns: (total_gbs, read_gbs, write_gbs)
                 R/W 分離不可の場合: (total, 0.0, 0.0)
        """
        if not self._mbm_total_files:
            return 0.0, 0.0, 0.0
        total_bytes = 0
        for f in self._mbm_total_files:
            try:
                total_bytes += int(f.read_text().strip())
            except (OSError, ValueError):
                return 0.0, 0.0, 0.0
        local_bytes = 0
        if self._mbm_rw_split:
            for f in self._mbm_local_files:
                try:
                    local_bytes += int(f.read_text().strip())
                except (OSError, ValueError):
                    pass

        now = time.monotonic()
        if self._mbm_prev_time == 0.0:
            self._mbm_prev_total = total_bytes
            self._mbm_prev_local = local_bytes
            self._mbm_prev_time = now
            return 0.0, 0.0, 0.0
        dt = now - self._mbm_prev_time
        if dt < 0.01:
            return 0.0, 0.0, 0.0

        d_total = max(total_bytes - self._mbm_prev_total, 0)
        d_local = max(local_bytes - self._mbm_prev_local, 0)
        self._mbm_prev_total = total_bytes
        self._mbm_prev_local = local_bytes
        self._mbm_prev_time = now

        gib = 1024 ** 3
        if self._mbm_rw_split:
            r_gbs = d_total / dt / gib   # total_bytes = reads
            w_gbs = d_local / dt / gib   # local_bytes = writes
            return r_gbs + w_gbs, r_gbs, w_gbs
        else:
            return d_total / dt / gib, 0.0, 0.0

    def _read_meminfo(self) -> dict[str, int]:
        if _IS_DARWIN:
            return self._read_meminfo_darwin()
        if _IS_WIN:
            return self._read_meminfo_win()
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

    @staticmethod
    def _read_meminfo_win() -> dict[str, int]:
        """Windows: ctypes GlobalMemoryStatusEx でメモリ情報を取得。"""
        info: dict[str, int] = {}
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
                ctypes.byref(ms)
            ):
                info["MemTotal"] = ms.ullTotalPhys // 1024
                info["MemFree"] = ms.ullAvailPhys // 1024
                info["Buffers"] = 0
                info["Cached"] = 0
                # Swap = PageFile - Physical (Windows page file includes phys mem)
                swap_total = ms.ullTotalPageFile - ms.ullTotalPhys
                swap_free = ms.ullAvailPageFile - ms.ullAvailPhys
                info["SwapTotal"] = max(swap_total, 0) // 1024
                info["SwapFree"] = max(swap_free, 0) // 1024
        except (OSError, AttributeError):
            pass
        return info

    def collect(self) -> tuple[MemoryUsage, SwapUsage]:
        m = self._read_meminfo()
        bw_total, bw_read, bw_write = self._read_mbm_bandwidth()

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
            bw_gbs=bw_total,
            bw_read_gbs=bw_read,
            bw_write_gbs=bw_write,
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
