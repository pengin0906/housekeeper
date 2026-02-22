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
    raid_level: str = ""              # md デバイスの場合: "raid0", "raid1" 等
    raid_members: list[str] = field(default_factory=list)
    raid_state: str = ""              # "clean", "active", "degraded" 等
    raid_member_of: str = ""          # メンバーの場合: 所属する md デバイス名

    @property
    def total_bytes_sec(self) -> float:
        return self.read_bytes_sec + self.write_bytes_sec

    @property
    def display_name(self) -> str:
        """表示用ラベル。RAID 情報付き。"""
        if self.raid_level:
            level = self.raid_level.upper().replace("RAID", "R")
            return f"{self.name} {level}×{len(self.raid_members)}"
        return self.name


# フィルタ: sd*, nvme*, vd*, md* のみ (パーティションは除外)
_DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|md\d+)$")


def _discover_md_arrays() -> dict[str, tuple[str, list[str], str]]:
    """sysfs から md RAID 情報を取得。

    Returns: {md_name: (level, [member_names], state)}
    """
    result: dict[str, tuple[str, list[str], str]] = {}
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
        # メンバー → md 逆引き
        self._member_to_md: dict[str, str] = {}
        for md_name, (_, members, _) in self._md_info.items():
            for m in members:
                self._member_to_md[m] = md_name

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

        # ソート: md デバイスを先頭に、その直後にメンバーを配置
        def _sort_key(d: DiskUsage) -> tuple[int, str, str]:
            if d.raid_level:
                return (0, d.name, "")
            if d.raid_member_of:
                return (0, d.raid_member_of, d.name)
            return (1, d.name, "")

        usages.sort(key=_sort_key)
        return usages
