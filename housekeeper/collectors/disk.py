"""Disk I/O collector - reads /proc/diskstats.

/proc/diskstats の各行:
  major minor name rd_ios rd_merges rd_sectors rd_ticks
                   wr_ios wr_merges wr_sectors wr_ticks
                   ios_in_prog io_ticks weighted_io_ticks
                   [discard fields...]

セクターサイズは 512 バイトとして計算する。
差分から read/write のバイト/秒とIOPS を取得。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass


@dataclass
class DiskStats:
    name: str
    rd_sectors: int = 0
    wr_sectors: int = 0
    rd_ios: int = 0
    wr_ios: int = 0


@dataclass
class DiskUsage:
    name: str
    read_bytes_sec: float = 0.0
    write_bytes_sec: float = 0.0
    read_iops: float = 0.0
    write_iops: float = 0.0

    @property
    def total_bytes_sec(self) -> float:
        return self.read_bytes_sec + self.write_bytes_sec


# フィルタ: sd*, nvme*, vd* のみ (パーティションは除外)
_DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$")


class DiskCollector:
    """Disk I/O コレクター。"""

    def __init__(self) -> None:
        self._prev: dict[str, DiskStats] = {}
        self._prev_time: float = 0.0

    def _read_diskstats(self) -> dict[str, DiskStats]:
        result: dict[str, DiskStats] = {}
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                name = parts[2]
                if not _DISK_RE.match(name):
                    continue
                result[name] = DiskStats(
                    name=name,
                    rd_ios=int(parts[3]),
                    rd_sectors=int(parts[5]),
                    wr_ios=int(parts[7]),
                    wr_sectors=int(parts[9]),
                )
        return result

    def collect(self) -> list[DiskUsage]:
        now = time.monotonic()
        curr = self._read_diskstats()
        dt = now - self._prev_time if self._prev_time else 0.0
        usages: list[DiskUsage] = []

        for name in sorted(curr.keys()):
            prev = self._prev.get(name)
            if prev is None or dt <= 0:
                usages.append(DiskUsage(name=name))
                continue

            d = curr[name]
            usages.append(DiskUsage(
                name=name,
                read_bytes_sec=512.0 * (d.rd_sectors - prev.rd_sectors) / dt,
                write_bytes_sec=512.0 * (d.wr_sectors - prev.wr_sectors) / dt,
                read_iops=(d.rd_ios - prev.rd_ios) / dt,
                write_iops=(d.wr_ios - prev.wr_ios) / dt,
            ))

        self._prev = curr
        self._prev_time = now
        return usages
