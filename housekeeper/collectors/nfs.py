"""NFS/SAN/NAS mount collector - /proc/mounts と mountstats から取得。

NFS マウントの I/O 状況を /proc/self/mountstats から取得。
CIFS (Samba), iSCSI 等のネットワークストレージも /proc/mounts から検出。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class NfsMountInfo:
    """ネットワークマウントの情報。"""
    device: str          # "server:/export" or "//server/share"
    mount_point: str
    fs_type: str         # nfs, nfs4, cifs, etc.
    # mountstats から取得 (NFS のみ)
    read_bytes: int = 0
    write_bytes: int = 0
    read_ops: int = 0
    write_ops: int = 0

    @property
    def short_device(self) -> str:
        d = self.device
        if len(d) > 25:
            parts = d.rsplit("/", 1)
            if len(parts) == 2:
                return f".../{parts[1]}"
        return d

    @property
    def type_label(self) -> str:
        if "nfs" in self.fs_type:
            return "NFS"
        if "cifs" in self.fs_type:
            return "SMB"
        if "iscsi" in self.fs_type or "fcoe" in self.fs_type:
            return "SAN"
        if "gluster" in self.fs_type:
            return "Gluster"
        if "ceph" in self.fs_type:
            return "Ceph"
        if "lustre" in self.fs_type:
            return "Lustre"
        if "9p" in self.fs_type:
            return "9P"
        return self.fs_type.upper()[:6]


@dataclass
class NfsMountUsage:
    """ネットワークマウントの使用状況 (差分)。"""
    device: str
    mount_point: str
    fs_type: str
    type_label: str
    read_bytes_sec: float = 0.0
    write_bytes_sec: float = 0.0
    read_iops: float = 0.0
    write_iops: float = 0.0


# ネットワークファイルシステムのタイプ
_NET_FS_TYPES = {"nfs", "nfs4", "nfs3", "cifs", "smbfs",
                 "glusterfs", "ceph", "lustre", "9p", "fuse.sshfs"}


class NfsMountCollector:
    """NFS/SAN/NAS マウントコレクター。"""

    def __init__(self) -> None:
        self._prev: dict[str, NfsMountInfo] = {}
        self._prev_time: float = 0.0

    def _read_net_mounts(self) -> list[NfsMountInfo]:
        """/proc/mounts からネットワークマウントを取得。"""
        mounts: list[NfsMountInfo] = []
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    device, mount_point, fs_type = parts[0], parts[1], parts[2]
                    if fs_type in _NET_FS_TYPES:
                        mounts.append(NfsMountInfo(
                            device=device,
                            mount_point=mount_point,
                            fs_type=fs_type,
                        ))
        except OSError:
            pass
        return mounts

    def _read_mountstats(self, mounts: list[NfsMountInfo]) -> None:
        """NFS マウントの /proc/self/mountstats から I/O 統計を読み取る。"""
        try:
            content = Path("/proc/self/mountstats").read_text()
        except (OSError, PermissionError):
            return

        mount_map = {m.mount_point: m for m in mounts}
        current_mount: NfsMountInfo | None = None

        for line in content.splitlines():
            stripped = line.strip()

            if stripped.startswith("device "):
                parts = stripped.split()
                # "device server:/path mounted on /mnt/xxx with fstype nfs4"
                for i, p in enumerate(parts):
                    if p == "on" and i + 1 < len(parts):
                        mp = parts[i + 1]
                        current_mount = mount_map.get(mp)
                        break

            elif current_mount and "READ:" in stripped:
                pass  # ops は次の行に
            elif current_mount and stripped.startswith("bytes:"):
                parts = stripped.split()
                try:
                    current_mount.read_bytes = int(parts[1])
                    current_mount.write_bytes = int(parts[2])
                except (IndexError, ValueError):
                    pass

    def collect(self) -> list[NfsMountUsage]:
        now = time.monotonic()
        dt = now - self._prev_time if self._prev_time else 0.0

        mounts = self._read_net_mounts()
        if not mounts:
            return []

        self._read_mountstats(mounts)

        usages: list[NfsMountUsage] = []
        for m in mounts:
            key = m.mount_point
            prev = self._prev.get(key)

            if prev is None or dt <= 0:
                usages.append(NfsMountUsage(
                    device=m.device,
                    mount_point=m.mount_point,
                    fs_type=m.fs_type,
                    type_label=m.type_label,
                ))
            else:
                usages.append(NfsMountUsage(
                    device=m.device,
                    mount_point=m.mount_point,
                    fs_type=m.fs_type,
                    type_label=m.type_label,
                    read_bytes_sec=max(0, (m.read_bytes - prev.read_bytes) / dt),
                    write_bytes_sec=max(0, (m.write_bytes - prev.write_bytes) / dt),
                ))

        self._prev = {m.mount_point: m for m in mounts}
        self._prev_time = now
        return usages
