"""PCIe bandwidth collector.

Linux: /sys/bus/pci/devices から PCIe 情報を取得。
macOS: system_profiler SPPCIDataType でPCIeデバイスを取得。
Windows: PCIe情報は取得困難なため空リストを返す。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


_IS_LINUX = sys.platform.startswith("linux")
_IS_DARWIN = sys.platform == "darwin"

# PCIe 世代ごとの per-lane 帯域 (GB/s, エンコーディングオーバーヘッド込み)
_PCIE_SPEED_GBS: dict[str, float] = {
    "2.5 GT/s":  0.25,   # Gen1
    "5.0 GT/s":  0.50,   # Gen2  (8b/10b)
    "5 GT/s":    0.50,
    "8.0 GT/s":  0.985,  # Gen3  (128b/130b)
    "8 GT/s":    0.985,
    "16.0 GT/s": 1.969,  # Gen4  (128b/130b)
    "16 GT/s":   1.969,
    "32.0 GT/s": 3.938,  # Gen5
    "32 GT/s":   3.938,
    "64.0 GT/s": 7.877,  # Gen6
    "64 GT/s":   7.877,
}

_PCIE_GEN_NAMES: dict[str, str] = {
    "2.5 GT/s": "Gen1",
    "5.0 GT/s": "Gen2",
    "5 GT/s":   "Gen2",
    "8.0 GT/s": "Gen3",
    "8 GT/s":   "Gen3",
    "16.0 GT/s": "Gen4",
    "16 GT/s":   "Gen4",
    "32.0 GT/s": "Gen5",
    "32 GT/s":   "Gen5",
    "64.0 GT/s": "Gen6",
    "64 GT/s":   "Gen6",
}


@dataclass
class PcieDeviceInfo:
    """PCIe デバイス情報。"""
    address: str            # BDF アドレス (e.g., "0000:01:00.0")
    name: str = "Unknown"
    vendor: str = ""
    current_speed: str = ""
    max_speed: str = ""
    current_width: int = 0
    max_width: int = 0
    device_type: str = ""   # "storage", "network", "display", "other"

    # 実 I/O スループット (対応サブシステムから取得)
    io_read_bytes_sec: float = 0.0
    io_write_bytes_sec: float = 0.0
    io_label: str = ""      # "nvme0n1", "enp210s0f0np0" 等

    @staticmethod
    def _normalize_speed(speed: str) -> str:
        """sysfs の速度文字列を正規化: "16.0 GT/s PCIe" -> "16.0 GT/s"。"""
        return speed.replace(" PCIe", "").strip()

    @property
    def gen_name(self) -> str:
        return _PCIE_GEN_NAMES.get(self._normalize_speed(self.current_speed),
                                    self.current_speed)

    @property
    def max_gen_name(self) -> str:
        return _PCIE_GEN_NAMES.get(self._normalize_speed(self.max_speed),
                                    self.max_speed)

    @property
    def current_bandwidth_gbs(self) -> float:
        per_lane = _PCIE_SPEED_GBS.get(self._normalize_speed(self.current_speed), 0.0)
        return per_lane * self.current_width

    @property
    def max_bandwidth_gbs(self) -> float:
        per_lane = _PCIE_SPEED_GBS.get(self._normalize_speed(self.max_speed), 0.0)
        return per_lane * self.max_width

    @property
    def link_utilization(self) -> float:
        """最大帯域に対する現在のリンク速度/幅の割合。"""
        if self.max_bandwidth_gbs <= 0:
            return 0.0
        return self.current_bandwidth_gbs / self.max_bandwidth_gbs

    @property
    def io_utilization(self) -> float:
        """現在の I/O スループットが理論帯域の何%か。"""
        bw = self.current_bandwidth_gbs
        if bw <= 0:
            return 0.0
        io_total_gbs = (self.io_read_bytes_sec + self.io_write_bytes_sec) / 1_073_741_824
        return min(io_total_gbs / bw, 1.0)

    @property
    def icon(self) -> str:
        """デバイスタイプに応じたアイコン。"""
        return {
            "display": "🖥",
            "storage": "💾",
            "network": "🌐",
        }.get(self.device_type, "")

    @property
    def short_name(self) -> str:
        n = self.name
        for prefix in [
            "NVIDIA Corporation ",
            "NVIDIA ",
            "Advanced Micro Devices, Inc. ",
            "Intel Corporation ",
        ]:
            n = n.removeprefix(prefix)
        # "(rev XX)" 除去
        n = re.sub(r"\s*\(rev [0-9a-fA-F]+\)\s*$", "", n)
        # [marketing name] があればそちらを優先
        m = re.search(r"\[(.+?)\]", n)
        if m:
            n = m.group(1)
        if len(n) > 30:
            n = n[:27] + "..."
        return n


def _read_sysfs(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (OSError, PermissionError):
        return ""


def _get_device_name(address: str) -> str:
    """lspci を使ってデバイス名を取得。"""
    try:
        result = subprocess.run(
            ["lspci", "-s", address, "-D"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            # "0000:01:00.0 3D controller: NVIDIA ..."
            parts = result.stdout.strip().split(": ", 1)
            return parts[1] if len(parts) > 1 else result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def _classify_device(class_top: int) -> str:
    """PCI クラスコードの上位バイトからデバイスタイプを返す。"""
    if class_top == 0x01:
        return "storage"
    elif class_top == 0x02:
        return "network"
    elif class_top == 0x03:
        return "display"
    elif class_top == 0x12:
        return "storage"  # processing accelerators として NVMe もある
    return "other"


class PcieCollector:
    """PCIe デバイス情報コレクター。

    主要なPCIeデバイス (GPU, NVMe, NIC) の
    リンクステータスと帯域情報、実 I/O スループットを収集する。
    """

    def __init__(self) -> None:
        self._device_names: dict[str, str] = {}
        self._device_subsystems: dict[str, tuple[str, str]] = {}  # BDF → (type, label)
        self._prev_disk: dict[str, tuple[int, int]] = {}  # name → (rd_sectors, wr_sectors)
        self._prev_net: dict[str, tuple[int, int]] = {}   # name → (rx_bytes, tx_bytes)
        self._prev_time: float = 0.0
        self._nvidia_pcie: bool = bool(shutil.which("nvidia-smi"))
        self._gpu_bdf_map: dict[str, int] = {}  # sysfs BDF → GPU index
        if _IS_LINUX:
            self._discover_subsystems()
            if self._nvidia_pcie:
                self._discover_nvidia_gpus()

    def _discover_subsystems(self) -> None:
        """PCIe BDF アドレスとサブシステムデバイス (NVMe, NIC) の対応付け。"""
        pci_path = Path("/sys/bus/pci/devices")
        if not pci_path.exists():
            return

        for dev_dir in pci_path.iterdir():
            address = dev_dir.name

            # NVMe: /sys/bus/pci/devices/XXXX/nvme/nvmeN
            nvme_dir = dev_dir / "nvme"
            if nvme_dir.exists():
                for nvme in nvme_dir.iterdir():
                    if nvme.name.startswith("nvme"):
                        # ブロックデバイス名 (nvme0n1 等)
                        blk_name = nvme.name + "n1"
                        self._device_subsystems[address] = ("storage", blk_name)
                        break
                continue

            # Network: /sys/bus/pci/devices/XXXX/net/ethX
            net_dir = dev_dir / "net"
            if net_dir.exists():
                for iface in net_dir.iterdir():
                    self._device_subsystems[address] = ("network", iface.name)
                    break

    def _discover_nvidia_gpus(self) -> None:
        """nvidia-smi から GPU index → sysfs BDF アドレスのマッピングを構築。"""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,gpu_bus_id",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return

        if result.returncode != 0:
            return

        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                idx = int(parts[0])
                # nvidia-smi: "00000000:D1:00.0" → sysfs: "0000:d1:00.0"
                raw_bdf = parts[1].strip()
                bdf = self._normalize_bdf(raw_bdf)
                self._gpu_bdf_map[bdf] = idx
                self._device_subsystems[bdf] = ("gpu", f"GPU{idx}")
            except (ValueError, IndexError):
                pass

    @staticmethod
    def _normalize_bdf(bdf: str) -> str:
        """nvidia-smi の BDF を sysfs 形式に正規化。

        "00000000:D1:00.0" → "0000:d1:00.0"
        "0000:d1:00.0"     → "0000:d1:00.0"
        """
        bdf = bdf.lower().strip()
        # 8桁ドメインを4桁に変換
        parts = bdf.split(":")
        if len(parts) >= 3 and len(parts[0]) == 8:
            parts[0] = parts[0][4:]  # 00000000 → 0000
            bdf = ":".join(parts)
        return bdf

    def _read_nvidia_pcie_throughput(self) -> dict[int, tuple[float, float]]:
        """nvidia-smi dmon から GPU ごとの PCIe RX/TX スループットを取得。

        Returns: {gpu_index: (rx_bytes_sec, tx_bytes_sec)}
        """
        if not self._nvidia_pcie:
            return {}

        try:
            result = subprocess.run(
                ["nvidia-smi", "dmon", "-s", "t", "-c", "1"],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {}

        if result.returncode != 0:
            return {}

        throughput: dict[int, tuple[float, float]] = {}
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            fields = line.split()
            if len(fields) < 3:
                continue
            try:
                idx = int(fields[0])
                # dmon -s t: rxpci (MB/s), txpci (MB/s)
                rx_mbs = float(fields[1])
                tx_mbs = float(fields[2])
                throughput[idx] = (rx_mbs * 1_048_576, tx_mbs * 1_048_576)
            except (ValueError, IndexError):
                pass

        return throughput

    def _read_disk_stats(self) -> dict[str, tuple[int, int]]:
        """diskstats から NVMe の読み書きセクターを取得。"""
        result: dict[str, tuple[int, int]] = {}
        try:
            with open("/proc/diskstats") as f:
                for line in f:
                    parts = line.split()
                    name = parts[2]
                    if name.startswith("nvme") and name.endswith("n1"):
                        rd_sectors = int(parts[5])
                        wr_sectors = int(parts[9])
                        result[name] = (rd_sectors, wr_sectors)
        except (OSError, IndexError, ValueError):
            pass
        return result

    def _read_net_stats(self) -> dict[str, tuple[int, int]]:
        """proc/net/dev からバイトカウンタを取得。"""
        result: dict[str, tuple[int, int]] = {}
        try:
            with open("/proc/net/dev") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    name, data = line.split(":", 1)
                    name = name.strip()
                    fields = data.split()
                    rx_bytes = int(fields[0])
                    tx_bytes = int(fields[8])
                    result[name] = (rx_bytes, tx_bytes)
        except (OSError, IndexError, ValueError):
            pass
        return result

    def collect(self) -> list[PcieDeviceInfo]:
        if not _IS_LINUX:
            # macOS: system_profiler は遅いので基本的には空を返す
            return []

        pci_path = Path("/sys/bus/pci/devices")
        if not pci_path.exists():
            return []

        now = time.monotonic()
        dt = now - self._prev_time if self._prev_time else 0.0

        # I/O カウンタ読み取り
        curr_disk = self._read_disk_stats()
        curr_net = self._read_net_stats()
        gpu_pcie = self._read_nvidia_pcie_throughput()

        devices: list[PcieDeviceInfo] = []

        for dev_dir in sorted(pci_path.iterdir()):
            address = dev_dir.name

            # PCIe リンクステータスファイルがあるデバイスのみ
            speed_file = dev_dir / "current_link_speed"
            width_file = dev_dir / "current_link_width"
            if not speed_file.exists():
                continue

            current_speed = _read_sysfs(speed_file)
            current_width_str = _read_sysfs(width_file)
            max_speed = _read_sysfs(dev_dir / "max_link_speed")
            max_width_str = _read_sysfs(dev_dir / "max_link_width")

            try:
                current_width = int(current_width_str)
            except ValueError:
                current_width = 0
            try:
                max_width = int(max_width_str)
            except ValueError:
                max_width = 0

            # クラスコード
            class_code = _read_sysfs(dev_dir / "class")
            if not class_code:
                continue

            try:
                cls = int(class_code, 16)
            except ValueError:
                continue

            class_top = (cls >> 16) & 0xFF
            # 0x03=Display, 0x01=Storage, 0x02=Network, 0x12=Processing accelerators
            if class_top not in (0x01, 0x02, 0x03, 0x12):
                continue

            device_type = _classify_device(class_top)

            # デバイス名の取得 (キャッシュ)
            if address not in self._device_names:
                self._device_names[address] = _get_device_name(address)
            name = self._device_names[address]

            # I/O スループット計算
            io_read = 0.0
            io_write = 0.0
            io_label = ""

            subsystem = self._device_subsystems.get(address)
            if subsystem:
                sub_type, sub_label = subsystem
                io_label = sub_label

                if sub_type == "storage" and sub_label in curr_disk and dt > 0:
                    rd_sectors, wr_sectors = curr_disk[sub_label]
                    prev = self._prev_disk.get(sub_label)
                    if prev is not None:
                        io_read = 512.0 * (rd_sectors - prev[0]) / dt
                        io_write = 512.0 * (wr_sectors - prev[1]) / dt

                elif sub_type == "network" and sub_label in curr_net and dt > 0:
                    rx_bytes, tx_bytes = curr_net[sub_label]
                    prev = self._prev_net.get(sub_label)
                    if prev is not None:
                        io_read = (rx_bytes - prev[0]) / dt   # RX
                        io_write = (tx_bytes - prev[1]) / dt  # TX

                elif sub_type == "gpu":
                    gpu_idx = self._gpu_bdf_map.get(address)
                    if gpu_idx is not None and gpu_idx in gpu_pcie:
                        io_read, io_write = gpu_pcie[gpu_idx]

            devices.append(PcieDeviceInfo(
                address=address,
                name=name,
                current_speed=current_speed,
                max_speed=max_speed,
                current_width=current_width,
                max_width=max_width,
                device_type=device_type,
                io_read_bytes_sec=max(io_read, 0.0),
                io_write_bytes_sec=max(io_write, 0.0),
                io_label=io_label,
            ))

        # 前回値を保存
        self._prev_disk = curr_disk
        self._prev_net = curr_net
        self._prev_time = now

        return devices
