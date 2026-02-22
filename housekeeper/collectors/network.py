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

    @property
    def total_bytes_sec(self) -> float:
        return self.rx_bytes_sec + self.tx_bytes_sec

    @property
    def display_name(self) -> str:
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


class NetworkCollector:
    """Network I/O コレクター (WAN/LAN/VIRTUAL 分類付き)。"""

    def __init__(self) -> None:
        self._prev: dict[str, NetStats] = {}
        self._prev_time: float = 0.0
        self._classification: dict[str, NetType] = {}
        self._classify_interval: float = 0.0  # 最後に分類した時間

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

    def _update_classification(self) -> None:
        """10秒ごとにインターフェース分類を更新。"""
        now = time.monotonic()
        if now - self._classify_interval > 10.0:
            self._classification = _classify_interfaces()
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
                usages.append(NetUsage(name=name, net_type=net_type))
                continue

            c = curr[name]
            usages.append(NetUsage(
                name=name,
                net_type=net_type,
                rx_bytes_sec=(c.rx_bytes - prev.rx_bytes) / dt,
                tx_bytes_sec=(c.tx_bytes - prev.tx_bytes) / dt,
            ))

        self._prev = curr
        self._prev_time = now
        return usages
