"""Kernel info collector - /proc から各種カーネル統計を取得。

取得する情報:
  - Load Average (1/5/15分) - /proc/loadavg
  - Uptime - /proc/uptime
  - Context Switches/sec - /proc/stat の ctxt
  - Interrupts/sec - /proc/stat の intr (合計)
  - Running/Total processes - /proc/loadavg
  - Kernel version - /proc/version
"""

from __future__ import annotations

import os
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
        """1分間ロードアベレージをCPU数で割った値 (1.0 = 100%)。"""
        return self.load_1 / self.num_cpus if self.num_cpus else self.load_1


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
        try:
            with open("/proc/version") as f:
                parts = f.read().split()
                # "Linux version 6.8.0-100-generic ..." -> "6.8.0-100-generic"
                return parts[2] if len(parts) > 2 else ""
        except OSError:
            return ""

    def collect(self) -> KernelInfo:
        now = time.monotonic()
        dt = now - self._prev_time if self._prev_time else 0.0

        # /proc/loadavg: "0.42 0.35 0.28 2/1234 56789"
        load_1 = load_5 = load_15 = 0.0
        running = total = 0
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
                load_1 = float(parts[0])
                load_5 = float(parts[1])
                load_15 = float(parts[2])
                procs = parts[3].split("/")
                running = int(procs[0])
                total = int(procs[1])
        except (OSError, ValueError, IndexError):
            pass

        # /proc/uptime: "123456.78 234567.89"
        uptime = 0.0
        try:
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass

        # /proc/stat: context switches と interrupts
        ctxt = 0
        intr = 0
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
