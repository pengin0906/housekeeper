"""CPU usage collector - reads /proc/stat.

/proc/stat の各行は以下の形式:
  cpu  user nice system idle iowait irq softirq steal guest guest_nice

各値は起動時からの累積 jiffies (1/100秒) なので、
2回の読み取り差分から使用率を計算する。
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


class CpuCollector:
    """CPU 使用率コレクター。

    /proc/stat を定期的に読み取り、前回との差分から使用率を計算する。
    per-core + 全体合計を返す。
    """

    def __init__(self) -> None:
        self._prev: dict[str, CpuTimes] = {}

    def _read_stat(self) -> dict[str, CpuTimes]:
        result: dict[str, CpuTimes] = {}
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break
                parts = line.split()
                name = parts[0]  # "cpu" or "cpu0", "cpu1", ...
                vals = [int(x) for x in parts[1:9]]
                result[name] = CpuTimes(*vals)
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
