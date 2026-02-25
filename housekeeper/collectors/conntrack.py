"""Per-IP traffic collector via ss (iproute2).

Linux: ``ss -tni state established`` で TCP 接続ごとの
bytes_sent / bytes_received を取得し、リモート IP ごとに集約する。
root 不要。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass

_RE_BYTES_SENT = re.compile(r"bytes_sent:(\d+)")
_RE_BYTES_RECV = re.compile(r"bytes_received:(\d+)")


@dataclass
class IpTraffic:
    """リモート IP ごとの集約トラフィック。"""
    remote_ip: str
    tx_bytes_sec: float = 0.0
    rx_bytes_sec: float = 0.0
    conn_count: int = 0

    @property
    def total_bytes_sec(self) -> float:
        return self.tx_bytes_sec + self.rx_bytes_sec


# connection key: (local_addr, local_port, remote_addr, remote_port)
_ConnKey = tuple[str, int, str, int]


class ConntrackCollector:
    """Per-IP traffic collector using ss (iproute2)."""

    def __init__(self, top_n: int = 10) -> None:
        self.top_n = top_n
        self._prev: dict[_ConnKey, tuple[int, int]] = {}
        self._prev_time: float = 0.0

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("ss"))

    # ------------------------------------------------------------------ #

    def _run_ss(self) -> str:
        try:
            r = subprocess.run(
                ["ss", "-tni", "state", "established"],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    @staticmethod
    def _parse_addr(addr: str) -> tuple[str, int]:
        """'192.168.0.1:443' or '[::1]:443' -> (ip, port)."""
        if addr.startswith("["):
            # IPv6: [addr]:port
            bracket = addr.index("]")
            return addr[1:bracket], int(addr[bracket + 2:])
        idx = addr.rfind(":")
        return addr[:idx], int(addr[idx + 1:])

    @staticmethod
    def _is_loopback(ip: str) -> bool:
        return ip.startswith("127.") or ip == "::1"

    def _parse_ss(self) -> dict[_ConnKey, tuple[int, int]]:
        """Parse ss output -> {conn_key: (bytes_sent, bytes_received)}."""
        out = self._run_ss()
        if not out:
            return {}

        result: dict[_ConnKey, tuple[int, int]] = {}
        lines = out.splitlines()
        i = 0
        # skip header
        if lines and lines[0].startswith("Recv-Q"):
            i = 1

        while i < len(lines):
            conn_line = lines[i].strip()
            i += 1
            if not conn_line:
                continue

            # info line(s): tab-indented
            info = ""
            while i < len(lines) and lines[i].startswith("\t"):
                info += lines[i]
                i += 1

            # parse connection line fields
            parts = conn_line.split()
            if len(parts) < 4:
                continue
            try:
                local_ip, local_port = self._parse_addr(parts[2])
                remote_ip, remote_port = self._parse_addr(parts[3])
            except (ValueError, IndexError):
                continue

            if self._is_loopback(remote_ip):
                continue
            # skip local-to-local (same host different port)
            if local_ip == remote_ip:
                continue

            m_sent = _RE_BYTES_SENT.search(info)
            m_recv = _RE_BYTES_RECV.search(info)
            sent = int(m_sent.group(1)) if m_sent else 0
            recv = int(m_recv.group(1)) if m_recv else 0

            key: _ConnKey = (local_ip, local_port, remote_ip, remote_port)
            result[key] = (sent, recv)

        return result

    # ------------------------------------------------------------------ #

    def collect(self) -> list[IpTraffic]:
        now = time.monotonic()
        dt = now - self._prev_time if self._prev_time else 0.0
        current = self._parse_ss()

        # per-connection delta -> aggregate by remote IP
        ip_agg: dict[str, list[float, float, int]] = {}

        for key, (sent, recv) in current.items():
            remote_ip = key[2]
            if remote_ip not in ip_agg:
                ip_agg[remote_ip] = [0.0, 0.0, 0]
            ip_agg[remote_ip][2] += 1

            if dt > 0 and key in self._prev:
                prev_s, prev_r = self._prev[key]
                ip_agg[remote_ip][0] += max(0, sent - prev_s)
                ip_agg[remote_ip][1] += max(0, recv - prev_r)

        self._prev = current
        self._prev_time = now

        usages: list[IpTraffic] = []
        for ip, (tx_d, rx_d, count) in ip_agg.items():
            usages.append(IpTraffic(
                remote_ip=ip,
                tx_bytes_sec=tx_d / dt if dt > 0 else 0.0,
                rx_bytes_sec=rx_d / dt if dt > 0 else 0.0,
                conn_count=count,
            ))

        usages.sort(key=lambda u: u.total_bytes_sec, reverse=True)
        return usages[:self.top_n] if self.top_n > 0 else usages
