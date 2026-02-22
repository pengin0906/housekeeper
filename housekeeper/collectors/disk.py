"""Disk I/O collector.

Linux: /proc/diskstats + /sys/block で RAID 検出。
macOS: iostat -d でディスク I/O 取得。
Windows: PowerShell Get-Counter でディスク統計取得 (ベストエフォート)。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


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

    # RAID メタデータ
    raid_level: str = ""
    raid_members: list[str] = field(default_factory=list)
    raid_state: str = ""
    raid_member_of: str = ""

    @property
    def total_bytes_sec(self) -> float:
        return self.read_bytes_sec + self.write_bytes_sec

    @property
    def display_name(self) -> str:
        if self.raid_level:
            level = self.raid_level.upper().replace("RAID", "R")
            return f"{self.name} {level}×{len(self.raid_members)}"
        return self.name


# フィルタ: sd*, nvme*, vd*, md* のみ (パーティションは除外)
_DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|md\d+)$")

_IS_DARWIN = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"


def _discover_md_arrays() -> dict[str, tuple[str, list[str], str]]:
    """sysfs から md RAID 情報を取得 (Linux only)。"""
    result: dict[str, tuple[str, list[str], str]] = {}
    if _IS_DARWIN or _IS_WIN:
        return result
    sys_block = Path("/sys/block")
    if not sys_block.exists():
        return result

    for md_dir in sorted(sys_block.iterdir()):
        if not md_dir.name.startswith("md"):
            continue
        md_meta = md_dir / "md"
        if not md_meta.exists():
            continue
        try:
            level = (md_meta / "level").read_text().strip()
        except OSError:
            continue
        try:
            state = (md_meta / "array_state").read_text().strip()
        except OSError:
            state = ""
        members: list[str] = []
        for dev in sorted(md_meta.iterdir()):
            if dev.name.startswith("dev-"):
                members.append(dev.name.removeprefix("dev-"))
        result[md_dir.name] = (level, members, state)

    return result


class DiskCollector:
    """Disk I/O コレクター。"""

    def __init__(self) -> None:
        self._prev: dict[str, DiskStats] = {}
        self._prev_time: float = 0.0
        self._md_info = _discover_md_arrays()
        self._member_to_md: dict[str, str] = {}
        for md_name, (_, members, _) in self._md_info.items():
            for m in members:
                self._member_to_md[m] = md_name

    def _read_diskstats(self) -> dict[str, DiskStats]:
        if _IS_DARWIN:
            return self._read_diskstats_darwin()
        if _IS_WIN:
            return self._read_diskstats_win()
        return self._read_diskstats_linux()

    def _read_diskstats_linux(self) -> dict[str, DiskStats]:
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

    def _read_diskstats_darwin(self) -> dict[str, DiskStats]:
        """macOS: iostat -d でディスク統計を取得。"""
        result: dict[str, DiskStats] = {}
        try:
            out = subprocess.run(
                ["iostat", "-d", "-K", "-c", "1"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode != 0:
                return result
            lines = out.stdout.strip().splitlines()
            if len(lines) < 3:
                return result
            headers = lines[0].split()
            data_line = lines[2].split() if len(lines) > 2 else []
            col = 0
            for dev_name in headers:
                if col + 2 >= len(data_line):
                    break
                try:
                    kbs = float(data_line[col + 2])
                    sectors = int(kbs * 2)
                    result[dev_name] = DiskStats(name=dev_name, rd_sectors=sectors)
                except (ValueError, IndexError):
                    pass
                col += 3
        except (OSError, subprocess.TimeoutExpired):
            pass
        return result

    def _read_diskstats_win(self) -> dict[str, DiskStats]:
        """Windows: PowerShell Get-Counter でディスク統計を取得。"""
        result: dict[str, DiskStats] = {}
        try:
            cmd = (
                "Get-Counter "
                "'\\PhysicalDisk(*)\\Disk Read Bytes/sec',"
                "'\\PhysicalDisk(*)\\Disk Write Bytes/sec' "
                "-SampleInterval 0 | "
                "Select-Object -ExpandProperty CounterSamples | "
                "ForEach-Object { $_.InstanceName + '|' + $_.Path + '|' + $_.CookedValue }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                for line in out.stdout.strip().splitlines():
                    parts = line.split("|")
                    if len(parts) >= 3:
                        name = parts[0].strip()
                        if name == "_total":
                            continue
                        path = parts[1].lower()
                        val = float(parts[2])
                        sectors = int(val / 512)
                        if name not in result:
                            result[name] = DiskStats(name=name)
                        if "read" in path:
                            result[name].rd_sectors = sectors
                        elif "write" in path:
                            result[name].wr_sectors = sectors
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        return result

    def collect(self) -> list[DiskUsage]:
        now = time.monotonic()
        curr = self._read_diskstats()
        dt = now - self._prev_time if self._prev_time else 0.0
        usages: list[DiskUsage] = []

        for name in sorted(curr.keys()):
            prev = self._prev.get(name)
            if prev is None or dt <= 0:
                du = DiskUsage(name=name)
            else:
                d = curr[name]
                du = DiskUsage(
                    name=name,
                    read_bytes_sec=512.0 * (d.rd_sectors - prev.rd_sectors) / dt,
                    write_bytes_sec=512.0 * (d.wr_sectors - prev.wr_sectors) / dt,
                    read_iops=(d.rd_ios - prev.rd_ios) / dt,
                    write_iops=(d.wr_ios - prev.wr_ios) / dt,
                )

            # RAID メタデータ付与
            if name in self._md_info:
                level, members, state = self._md_info[name]
                du.raid_level = level
                du.raid_members = members
                du.raid_state = state
            elif name in self._member_to_md:
                du.raid_member_of = self._member_to_md[name]

            usages.append(du)

        self._prev = curr
        self._prev_time = now

        def _sort_key(d: DiskUsage) -> tuple[int, str, str]:
            if d.raid_level:
                return (0, d.name, "")
            if d.raid_member_of:
                return (0, d.raid_member_of, d.name)
            return (1, d.name, "")

        usages.sort(key=_sort_key)
        return usages
