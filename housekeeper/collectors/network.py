"""Network I/O collector.

Linux: /proc/net/dev + /sys/class/net で WAN/LAN/VIRTUAL 分類。
macOS: netstat -ib でインターフェース統計。
Windows: PowerShell Get-NetAdapterStatistics で取得。
"""

from __future__ import annotations

import ipaddress
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class NetType(Enum):
    WAN = "WAN"
    LAN = "LAN"
    VIRTUAL = "VIR"
    UNKNOWN = "???"


@dataclass
class NetStats:
    name: str
    rx_bytes: int = 0
    tx_bytes: int = 0


@dataclass
class NetUsage:
    name: str
    net_type: NetType = NetType.UNKNOWN
    rx_bytes_sec: float = 0.0
    tx_bytes_sec: float = 0.0

    # Bonding / LACP メタデータ
    bond_mode: str = ""
    bond_members: list[str] | None = None
    bond_member_of: str = ""

    @property
    def total_bytes_sec(self) -> float:
        return self.rx_bytes_sec + self.tx_bytes_sec

    @property
    def display_name(self) -> str:
        if self.bond_mode:
            mode = self.bond_mode.split()[0] if self.bond_mode else ""
            n = len(self.bond_members) if self.bond_members else 0
            return f"[{self.net_type.value}]{self.name} {mode}×{n}"
        return f"[{self.net_type.value}]{self.name}"


_IS_DARWIN = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"

_VIRTUAL_PREFIXES = ("docker", "veth", "br-", "virbr", "lxc", "flannel",
                     "cni", "calico", "tun", "tap")

# macOS: 表示不要な仮想/システムインターフェース
_DARWIN_SKIP_PREFIXES = (
    "utun",    # VPN / iCloud Private Relay トンネル
    "awdl",    # AirDrop Wireless Direct Link
    "llw",     # Low Latency WLAN
    "anpi",    # Apple 内部
    "bridge",  # Thunderbolt ブリッジ
    "gif",     # IPv6 トンネル
    "stf",     # 6to4 トンネル
    "ap",      # アクセスポイント
    "pktap",   # Packet tap
    "ipsec",   # IPsec
    "XHC",     # USB Host Controller
    "vmnet",   # VMware
)


def _classify_darwin() -> dict[str, NetType]:
    """macOS: インターフェースを WAN/LAN に分類。"""
    classification: dict[str, NetType] = {}
    try:
        # デフォルトルート (WAN) のインターフェースを取得
        out = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=3,
        )
        default_iface = ""
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                if "interface:" in line:
                    default_iface = line.split(":")[-1].strip()
                    break
        # netstat -ib で存在するインターフェースを列挙
        out2 = subprocess.run(
            ["netstat", "-ib"],
            capture_output=True, text=True, timeout=3,
        )
        if out2.returncode == 0:
            seen: set[str] = set()
            for line in out2.stdout.splitlines()[1:]:
                parts = line.split()
                if not parts:
                    continue
                iface = parts[0]
                if iface in seen or iface.startswith("lo"):
                    continue
                if any(iface.startswith(p) for p in _DARWIN_SKIP_PREFIXES):
                    continue
                seen.add(iface)
                if iface == default_iface:
                    classification[iface] = NetType.WAN
                elif iface.startswith("en"):
                    classification[iface] = NetType.LAN
                else:
                    classification[iface] = NetType.UNKNOWN
    except (OSError, subprocess.TimeoutExpired):
        pass
    return classification


def _classify_interfaces() -> dict[str, NetType]:
    if _IS_DARWIN:
        return _classify_darwin()
    if _IS_WIN:
        return {}

    classification: dict[str, NetType] = {}
    default_ifaces: set[str] = set()
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000":
                    default_ifaces.add(parts[0])
    except OSError:
        pass

    try:
        net_path = Path("/sys/class/net")
        for iface_dir in sorted(net_path.iterdir()):
            iface = iface_dir.name
            if iface == "lo":
                continue
            if any(iface.startswith(p) for p in _VIRTUAL_PREFIXES):
                classification[iface] = NetType.VIRTUAL
                continue
            if iface in default_ifaces:
                classification[iface] = NetType.WAN
            else:
                ip_addr = _get_iface_ip(iface)
                if ip_addr:
                    try:
                        addr = ipaddress.ip_address(ip_addr)
                        if addr.is_private:
                            classification[iface] = NetType.LAN
                        else:
                            classification[iface] = NetType.WAN
                    except ValueError:
                        classification[iface] = NetType.LAN
                else:
                    classification[iface] = NetType.LAN
    except OSError:
        pass

    return classification


def _get_iface_ip(iface: str) -> str | None:
    try:
        with open("/proc/net/fib_trie") as f:
            f.read()
        return None
    except OSError:
        return None


def _discover_bonds() -> dict[str, tuple[str, list[str]]]:
    """sysfs から bonding 情報取得 (Linux only)。"""
    result: dict[str, tuple[str, list[str]]] = {}
    if _IS_DARWIN or _IS_WIN:
        return result
    net_path = Path("/sys/class/net")
    if not net_path.exists():
        return result

    for iface_dir in sorted(net_path.iterdir()):
        bonding_dir = iface_dir / "bonding"
        if not bonding_dir.exists():
            continue
        try:
            mode = (bonding_dir / "mode").read_text().strip()
        except OSError:
            mode = ""
        try:
            slaves_text = (bonding_dir / "slaves").read_text().strip()
            members = slaves_text.split() if slaves_text else []
        except OSError:
            members = []
        result[iface_dir.name] = (mode, members)

    return result


class NetworkCollector:
    """Network I/O コレクター。"""

    def __init__(self) -> None:
        self._prev: dict[str, NetStats] = {}
        self._prev_time: float = 0.0
        self._classification: dict[str, NetType] = {}
        self._classify_interval: float = 0.0
        self._bond_info: dict[str, tuple[str, list[str]]] = {}
        self._member_to_bond: dict[str, str] = {}
        self._update_bonds()

    def _read_netdev(self) -> dict[str, NetStats]:
        if _IS_DARWIN:
            return self._read_netdev_darwin()
        if _IS_WIN:
            return self._read_netdev_win()
        return self._read_netdev_linux()

    def _read_netdev_linux(self) -> dict[str, NetStats]:
        result: dict[str, NetStats] = {}
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                iface, data = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo":
                    continue
                parts = data.split()
                result[iface] = NetStats(
                    name=iface,
                    rx_bytes=int(parts[0]),
                    tx_bytes=int(parts[8]),
                )
        return result

    def _read_netdev_darwin(self) -> dict[str, NetStats]:
        """macOS: netstat -ib でインターフェース統計。"""
        result: dict[str, NetStats] = {}
        try:
            out = subprocess.run(
                ["netstat", "-ib"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode != 0:
                return result
            for line in out.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 10:
                    continue
                iface = parts[0]
                if iface.startswith("lo"):
                    continue
                # macOS 仮想/システムインターフェースをスキップ
                if any(iface.startswith(p) for p in _DARWIN_SKIP_PREFIXES):
                    continue
                try:
                    ibytes = int(parts[6])
                    obytes = int(parts[9])
                    # トラフィックゼロのインターフェースをスキップ
                    if ibytes == 0 and obytes == 0:
                        continue
                    if iface in result:
                        if ibytes > result[iface].rx_bytes:
                            result[iface] = NetStats(name=iface, rx_bytes=ibytes, tx_bytes=obytes)
                    else:
                        result[iface] = NetStats(name=iface, rx_bytes=ibytes, tx_bytes=obytes)
                except (ValueError, IndexError):
                    continue
        except (OSError, subprocess.TimeoutExpired):
            pass
        return result

    def _read_netdev_win(self) -> dict[str, NetStats]:
        """Windows: PowerShell で取得。"""
        result: dict[str, NetStats] = {}
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetAdapterStatistics | "
                 "ForEach-Object { $_.Name + '|' + $_.ReceivedBytes + '|' + $_.SentBytes }"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                for line in out.stdout.strip().splitlines():
                    parts = line.split("|")
                    if len(parts) >= 3:
                        name = parts[0].strip()
                        rx = int(parts[1])
                        tx = int(parts[2])
                        result[name] = NetStats(name=name, rx_bytes=rx, tx_bytes=tx)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        return result

    def _update_bonds(self) -> None:
        self._bond_info = _discover_bonds()
        self._member_to_bond = {}
        for bond_name, (_, members) in self._bond_info.items():
            for m in members:
                self._member_to_bond[m] = bond_name

    def _update_classification(self) -> None:
        now = time.monotonic()
        if now - self._classify_interval > 10.0:
            self._classification = _classify_interfaces()
            self._update_bonds()
            self._classify_interval = now

    def collect(self) -> list[NetUsage]:
        self._update_classification()

        now = time.monotonic()
        curr = self._read_netdev()
        dt = now - self._prev_time if self._prev_time else 0.0
        usages: list[NetUsage] = []

        type_order = {NetType.WAN: 0, NetType.LAN: 1, NetType.VIRTUAL: 2, NetType.UNKNOWN: 3}

        for name in sorted(curr.keys(),
                           key=lambda n: (type_order.get(self._classification.get(n, NetType.UNKNOWN), 3), n)):
            net_type = self._classification.get(name, NetType.UNKNOWN)
            prev = self._prev.get(name)
            if prev is None or dt <= 0:
                nu = NetUsage(name=name, net_type=net_type)
            else:
                c = curr[name]
                nu = NetUsage(
                    name=name,
                    net_type=net_type,
                    rx_bytes_sec=(c.rx_bytes - prev.rx_bytes) / dt,
                    tx_bytes_sec=(c.tx_bytes - prev.tx_bytes) / dt,
                )

            if name in self._bond_info:
                mode, members = self._bond_info[name]
                nu.bond_mode = mode
                nu.bond_members = members
            elif name in self._member_to_bond:
                nu.bond_member_of = self._member_to_bond[name]

            usages.append(nu)

        if self._bond_info:
            def _sort_key(n: NetUsage) -> tuple[int, str, int, str]:
                t = type_order.get(n.net_type, 3)
                if n.bond_mode:
                    return (t, n.name, 0, "")
                if n.bond_member_of:
                    bt = type_order.get(
                        self._classification.get(n.bond_member_of, NetType.UNKNOWN), 3)
                    return (bt, n.bond_member_of, 1, n.name)
                return (t, n.name, 0, "")
            usages.sort(key=_sort_key)

        self._prev = curr
        self._prev_time = now
        return usages
