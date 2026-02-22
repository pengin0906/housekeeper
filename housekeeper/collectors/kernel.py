"""Kernel info collector.

Linux: /proc/loadavg, /proc/uptime, /proc/stat, /proc/version
macOS: os.getloadavg(), sysctl kern.boottime, platform.release()
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass
class KernelInfo:
    """カーネル統計情報。"""
    load_1: float = 0.0
    load_5: float = 0.0
    load_15: float = 0.0
    uptime_sec: float = 0.0
    running_procs: int = 0
    total_procs: int = 0
    ctx_switches_sec: float = 0.0
    interrupts_sec: float = 0.0
    kernel_version: str = ""
    num_cpus: int = 1

    @property
    def uptime_str(self) -> str:
        s = int(self.uptime_sec)
        days = s // 86400
        hours = (s % 86400) // 3600
        mins = (s % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h {mins}m"
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"

    @property
    def load_per_cpu(self) -> float:
        return self.load_1 / self.num_cpus if self.num_cpus else self.load_1


_IS_DARWIN = sys.platform == "darwin"


class KernelCollector:
    """カーネル情報コレクター。"""

    def __init__(self) -> None:
        self._prev_ctxt: int = 0
        self._prev_intr: int = 0
        self._prev_time: float = 0.0
        self._num_cpus = os.cpu_count() or 1
        self._kernel_version = self._read_kernel_version()

    @staticmethod
    def _read_kernel_version() -> str:
        if _IS_DARWIN:
            return platform.release()
        try:
            with open("/proc/version") as f:
                parts = f.read().split()
                return parts[2] if len(parts) > 2 else ""
        except OSError:
            return platform.release()

    def collect(self) -> KernelInfo:
        now = time.monotonic()
        dt = now - self._prev_time if self._prev_time else 0.0

        # Load average
        load_1 = load_5 = load_15 = 0.0
        running = total = 0
        try:
            load_1, load_5, load_15 = os.getloadavg()
        except OSError:
            pass

        # Running/total procs (Linux only from /proc/loadavg)
        if not _IS_DARWIN:
            try:
                with open("/proc/loadavg") as f:
                    parts = f.read().split()
                    procs = parts[3].split("/")
                    running = int(procs[0])
                    total = int(procs[1])
            except (OSError, ValueError, IndexError):
                pass

        # Uptime
        uptime = 0.0
        if _IS_DARWIN:
            uptime = self._read_uptime_darwin()
        else:
            try:
                with open("/proc/uptime") as f:
                    uptime = float(f.read().split()[0])
            except (OSError, ValueError, IndexError):
                pass

        # Context switches / interrupts (Linux only)
        ctxt = 0
        intr = 0
        if not _IS_DARWIN:
            try:
                with open("/proc/stat") as f:
                    for line in f:
                        if line.startswith("ctxt "):
                            ctxt = int(line.split()[1])
                        elif line.startswith("intr "):
                            intr = int(line.split()[1])
            except (OSError, ValueError, IndexError):
                pass

        ctx_sec = 0.0
        intr_sec = 0.0
        if dt > 0 and self._prev_time > 0:
            ctx_sec = (ctxt - self._prev_ctxt) / dt
            intr_sec = (intr - self._prev_intr) / dt

        self._prev_ctxt = ctxt
        self._prev_intr = intr
        self._prev_time = now

        return KernelInfo(
            load_1=load_1,
            load_5=load_5,
            load_15=load_15,
            uptime_sec=uptime,
            running_procs=running,
            total_procs=total,
            ctx_switches_sec=ctx_sec,
            interrupts_sec=intr_sec,
            kernel_version=self._kernel_version,
            num_cpus=self._num_cpus,
        )

    @staticmethod
    def _read_uptime_darwin() -> float:
        """macOS: sysctl kern.boottime からアップタイムを計算。"""
        try:
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0:
                # "{ sec = 1708123456, usec = 123456 } ..."
                text = out.stdout.strip()
                sec_part = text.split("sec =")[1].split(",")[0].strip()
                boot_time = int(sec_part)
                return time.time() - boot_time
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            pass
        return 0.0
