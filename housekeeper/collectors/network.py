"""Network I/O collector - reads /proc/net/dev.

/proc/net/dev の各行 (ヘッダ2行の後):
  iface: rx_bytes rx_packets ... tx_bytes tx_packets ...

差分から受信/送信のバイト/秒を計算する。
lo (loopback) は除外。

インターフェースの分類:
  - WAN (インターネット向け): デフォルトルートを持つインターフェース
  - LAN (ローカル): プライベートアドレス (10.x, 172.16-31.x, 192.168.x)
  - Virtual: docker*, veth*, br-*, virbr* 等
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class NetType(Enum):
    WAN = "WAN"       # デフォルトルートを持つ (インターネット向け)
    LAN = "LAN"       # ローカルネットワーク
    VIRTUAL = "VIR"   # Docker, bridge 等の仮想インターフェース
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
    bond_mode: str = ""               # "802.3ad", "balance-rr" 等
    bond_members: list[str] | None = None
    bond_member_of: str = ""          # メンバーの場合: 所属するボンドIF名

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


# 仮想インターフェースの判別パターン
_VIRTUAL_PREFIXES = ("docker", "veth", "br-", "virbr", "lxc", "flannel",
                     "cni", "calico", "tun", "tap")


def _classify_interfaces() -> dict[str, NetType]:
    """各ネットワークインターフェースを WAN/LAN/VIRTUAL に分類する。"""
    classification: dict[str, NetType] = {}

    # デフォルトルートを持つインターフェースを特定
    default_ifaces: set[str] = set()
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000":
                    default_ifaces.add(parts[0])
    except OSError:
        pass

    # 各インターフェースの IP アドレスを取得して分類
    try:
        net_path = Path("/sys/class/net")
        for iface_dir in sorted(net_path.iterdir()):
            iface = iface_dir.name
            if iface == "lo":
                continue

            # 仮想インターフェースの判別
            if any(iface.startswith(p) for p in _VIRTUAL_PREFIXES):
                classification[iface] = NetType.VIRTUAL
                continue

            # /sys/class/net/<iface>/type でタイプ確認
            # type 772 = loopback, 他にも仮想があるがここでは省略

            if iface in default_ifaces:
                classification[iface] = NetType.WAN
            else:
                # IP アドレスからプライベートかどうか判定
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
    """インターフェースの IPv4 アドレスを取得 (/proc/net/fib_trie は複雑なので
    /sys/class/net/<iface>/address ではなく ip コマンド不要の方法で)。"""
    try:
        # /proc/net/if_inet6 で IPv6 を見ることもできるが、
        # ここでは /proc/net/fib_trie からの簡易取得を試みる
        with open("/proc/net/fib_trie") as f:
            content = f.read()
        # 簡易: 正確にはパースが必要だが、ここではフォールバック
        return None
    except OSError:
        return None


def _discover_bonds() -> dict[str, tuple[str, list[str]]]:
    """sysfs から bonding インターフェース情報を取得。

    Returns: {bond_name: (mode, [member_names])}
    """
    result: dict[str, tuple[str, list[str]]] = {}
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
    """Network I/O コレクター (WAN/LAN/VIRTUAL 分類付き)。"""

    def __init__(self) -> None:
        self._prev: dict[str, NetStats] = {}
        self._prev_time: float = 0.0
        self._classification: dict[str, NetType] = {}
        self._classify_interval: float = 0.0  # 最後に分類した時間
        self._bond_info: dict[str, tuple[str, list[str]]] = {}
        self._member_to_bond: dict[str, str] = {}
        self._update_bonds()

    def _read_netdev(self) -> dict[str, NetStats]:
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

    def _update_bonds(self) -> None:
        """ボンディング情報を更新。"""
        self._bond_info = _discover_bonds()
        self._member_to_bond = {}
        for bond_name, (_, members) in self._bond_info.items():
            for m in members:
                self._member_to_bond[m] = bond_name

    def _update_classification(self) -> None:
        """10秒ごとにインターフェース分類とボンディング情報を更新。"""
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

        # WAN → LAN → VIRTUAL の順にソート
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

            # ボンディングメタデータ付与
            if name in self._bond_info:
                mode, members = self._bond_info[name]
                nu.bond_mode = mode
                nu.bond_members = members
            elif name in self._member_to_bond:
                nu.bond_member_of = self._member_to_bond[name]

            usages.append(nu)

        # ボンドIF の直後にメンバーを配置
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
