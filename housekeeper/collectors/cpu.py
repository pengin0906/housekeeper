"""CPU usage collector.

Linux: /proc/stat から累積 jiffies を読み取り、差分で使用率を計算。
macOS: Mach host_processor_info() で per-core CPU ticks を直接取得 (ctypes)。
Windows: PowerShell Get-Counter でプロセッサ使用率を取得。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class CpuTimes:
    user: int = 0
    nice: int = 0
    system: int = 0
    idle: int = 0
    iowait: int = 0
    irq: int = 0
    softirq: int = 0
    steal: int = 0

    @property
    def total(self) -> int:
        return (
            self.user + self.nice + self.system + self.idle
            + self.iowait + self.irq + self.softirq + self.steal
        )

    @property
    def busy(self) -> int:
        return self.total - self.idle - self.iowait


@dataclass
class CpuUsage:
    """1回の差分から計算された CPU 使用率。"""
    label: str
    user_pct: float = 0.0
    nice_pct: float = 0.0
    system_pct: float = 0.0
    iowait_pct: float = 0.0
    irq_pct: float = 0.0
    steal_pct: float = 0.0
    idle_pct: float = 0.0

    @property
    def total_pct(self) -> float:
        return 100.0 - self.idle_pct


_IS_DARWIN = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"

# ─── macOS Mach API (ctypes) ───────────────────────────────────
# host_processor_info() で per-core CPU ticks を直接取得。
# subprocess を使わないため ~0.1ms で完了する (top -l 1 は ~800ms)。

# Mach 定数
_CPU_STATE_USER = 0
_CPU_STATE_SYSTEM = 1
_CPU_STATE_IDLE = 2
_CPU_STATE_NICE = 3
_CPU_STATE_MAX = 4  # ticks per core
_PROCESSOR_CPU_LOAD_INFO = 2
_HOST_PRIV_NULL = 0

_libc = None


def _darwin_host_processor_info() -> dict[str, CpuTimes]:
    """Mach host_processor_info で per-core CPU ticks を取得 (macOS 専用)。"""
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    libc = _libc

    # mach_host_self()
    host = libc.mach_host_self()

    processor_count = ctypes.c_uint(0)
    info_array = ctypes.POINTER(ctypes.c_int)()
    info_count = ctypes.c_uint(0)

    # host_processor_info(host, PROCESSOR_CPU_LOAD_INFO, &count, &info, &info_cnt)
    kr = libc.host_processor_info(
        host,
        ctypes.c_int(_PROCESSOR_CPU_LOAD_INFO),
        ctypes.byref(processor_count),
        ctypes.byref(info_array),
        ctypes.byref(info_count),
    )
    if kr != 0:
        return {}

    n_cpus = processor_count.value
    result: dict[str, CpuTimes] = {}
    total_user = total_nice = total_system = total_idle = 0

    for i in range(n_cpus):
        base = i * _CPU_STATE_MAX
        user = info_array[base + _CPU_STATE_USER]
        system = info_array[base + _CPU_STATE_SYSTEM]
        idle = info_array[base + _CPU_STATE_IDLE]
        nice = info_array[base + _CPU_STATE_NICE]

        result[f"cpu{i}"] = CpuTimes(
            user=user, nice=nice, system=system, idle=idle,
        )
        total_user += user
        total_nice += nice
        total_system += system
        total_idle += idle

    # 合計
    result["cpu"] = CpuTimes(
        user=total_user, nice=total_nice,
        system=total_system, idle=total_idle,
    )

    # メモリ解放: vm_deallocate(mach_task_self(), info_array, info_count * sizeof(int))
    try:
        libc.vm_deallocate(
            libc.mach_task_self(),
            info_array,
            ctypes.c_uint(info_count.value * ctypes.sizeof(ctypes.c_int)),
        )
    except Exception:
        pass

    return result


class CpuCollector:
    """CPU 使用率コレクター。"""

    def __init__(self) -> None:
        self._prev: dict[str, CpuTimes] = {}

    def _read_stat(self) -> dict[str, CpuTimes]:
        if _IS_DARWIN:
            return self._read_stat_darwin()
        if _IS_WIN:
            return self._read_stat_win()
        return self._read_stat_linux()

    def _read_stat_linux(self) -> dict[str, CpuTimes]:
        result: dict[str, CpuTimes] = {}
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break
                parts = line.split()
                name = parts[0]
                vals = [int(x) for x in parts[1:9]]
                result[name] = CpuTimes(*vals)
        return result

    def _read_stat_darwin(self) -> dict[str, CpuTimes]:
        """macOS: Mach host_processor_info で per-core CPU ticks を取得。"""
        result: dict[str, CpuTimes] = {}
        try:
            result = _darwin_host_processor_info()
        except Exception:
            pass
        if not result:
            # フォールバック: top -l 1 で概算 (遅い)
            try:
                out = subprocess.run(
                    ["top", "-l", "1", "-n", "0", "-s", "0"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in out.stdout.splitlines():
                    if "CPU usage:" in line:
                        user = sys_pct = idle = 0.0
                        for part in line.split(","):
                            part = part.strip()
                            if "user" in part:
                                user = float(part.split("%")[0].split()[-1])
                            elif "sys" in part:
                                sys_pct = float(part.split("%")[0].split()[-1])
                            elif "idle" in part:
                                idle = float(part.split("%")[0].split()[-1])
                        scale = 10000
                        result["cpu"] = CpuTimes(
                            user=int(user * scale),
                            system=int(sys_pct * scale),
                            idle=int(idle * scale),
                        )
                        break
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        return result

    def _read_stat_win(self) -> dict[str, CpuTimes]:
        """Windows: ctypes GetSystemTimes で累積 CPU ticks を取得。"""
        result: dict[str, CpuTimes] = {}
        try:
            import ctypes
            idle = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            if ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                # kernel には idle を含む → system = kernel - idle
                result["cpu"] = CpuTimes(
                    user=user.value,
                    system=kernel.value - idle.value,
                    idle=idle.value,
                )
        except (OSError, AttributeError):
            pass
        return result

    def collect(self) -> list[CpuUsage]:
        curr = self._read_stat()
        usages: list[CpuUsage] = []

        for name in sorted(curr.keys(), key=lambda x: (len(x), x)):
            ct = curr[name]
            prev = self._prev.get(name)
            if prev is None:
                usages.append(CpuUsage(label=name))
                continue

            dt = ct.total - prev.total
            if dt == 0:
                usages.append(CpuUsage(label=name))
                continue

            usages.append(CpuUsage(
                label=name,
                user_pct=100.0 * (ct.user - prev.user) / dt,
                nice_pct=100.0 * (ct.nice - prev.nice) / dt,
                system_pct=100.0 * (ct.system - prev.system) / dt,
                iowait_pct=100.0 * (ct.iowait - prev.iowait) / dt,
                irq_pct=100.0 * ((ct.irq + ct.softirq) - (prev.irq + prev.softirq)) / dt,
                steal_pct=100.0 * (ct.steal - prev.steal) / dt,
                idle_pct=100.0 * ((ct.idle + ct.iowait) - (prev.idle + prev.iowait)) / dt,
            ))

        self._prev = curr
        return usages
