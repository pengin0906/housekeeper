"""housekeeper - メインエントリポイント。

アーキテクチャ:
  1. 起動時に利用可能なアクセラレータを検出
  2. 必要なコレクターだけを遅延ロード (importlib)
  3. curses TUI ループで定期的にデータ収集 → 描画

GPU がない環境では GPU 関連モジュールは一切 import されない。
外部依存ゼロ (標準ライブラリのみ) で動作する。
SATA/NVMe/100GbE 等は接続されていなくてもコードは含まれる
(存在しないデバイスは自動的にスキップ)。
"""

from __future__ import annotations

import argparse
import curses
import importlib
import shutil
import sys
import time
from pathlib import Path


def _detect_accelerators() -> dict[str, bool]:
    """利用可能なアクセラレータを検出する (コマンドの存在確認のみ)。"""
    accel = {
        "nvidia": bool(shutil.which("nvidia-smi")),
        "amd":    bool(shutil.which("rocm-smi")),
        "gaudi":  bool(shutil.which("hl-smi")),
    }
    # Apple Silicon GPU (Metal) — macOS のみ
    if sys.platform == "darwin":
        try:
            AppleGpuCollector = _lazy_import(
                "housekeeper.collectors.apple_gpu", "AppleGpuCollector")
            accel["apple"] = AppleGpuCollector.available()
        except Exception:
            accel["apple"] = False
    else:
        accel["apple"] = False
    return accel


def _has_pcie_devices() -> bool:
    """PCIe デバイスが存在するか。"""
    if sys.platform.startswith("linux"):
        return Path("/sys/bus/pci/devices").exists()
    # macOS/Windows: PCIe情報は別の方法で取得するが、現状は非対応
    return False


def _has_net_mounts() -> bool:
    """NFS/CIFS 等のネットワークマウントがあるか。"""
    net_fs = {"nfs", "nfs4", "nfs3", "cifs", "smbfs", "glusterfs",
              "ceph", "lustre", "9p", "fuse.sshfs"}
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] in net_fs:
                        return True
        except OSError:
            pass
    elif sys.platform == "darwin":
        import subprocess
        try:
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=3)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    lower = line.lower()
                    if "nfs" in lower or "smbfs" in lower or "cifs" in lower:
                        return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "win32":
        import subprocess
        try:
            out = subprocess.run(["net", "use"], capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and ("OK" in out.stdout or "Disconnected" in out.stdout):
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


def _lazy_import(module_path: str, class_name: str):
    """モジュールを遅延ロードしてクラスを返す。"""
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _run_tui(stdscr: curses.window, args: argparse.Namespace) -> None:
    """curses TUI メインループ。"""
    # curses 初期設定
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(int(args.interval * 1000))

    from housekeeper.ui.colors import init_colors
    from housekeeper.ui.renderer import Renderer
    init_colors()

    renderer = Renderer(show_per_core=not args.no_per_core)

    # === 常時ロードするコレクター (/proc のみ、軽量) ===
    from housekeeper.collectors.cpu import CpuCollector
    from housekeeper.collectors.memory import MemoryCollector
    from housekeeper.collectors.disk import DiskCollector
    from housekeeper.collectors.network import NetworkCollector
    from housekeeper.collectors.process import ProcessCollector
    from housekeeper.collectors.kernel import KernelCollector

    cpu_col = CpuCollector()
    mem_col = MemoryCollector()
    disk_col = DiskCollector()
    net_col = NetworkCollector()
    proc_col = ProcessCollector(top_n=0)
    kern_col = KernelCollector()

    # === 条件付きコレクター (遅延ロード) ===
    accel = _detect_accelerators()

    nvidia_col = None
    amd_col = None
    gaudi_col = None
    apple_col = None
    gpu_proc_col = None
    pcie_col = None
    nfs_col = None

    if accel["nvidia"] and not args.no_gpu:
        GpuCollector = _lazy_import("housekeeper.collectors.gpu", "GpuCollector")
        nvidia_col = GpuCollector()
        GpuProcessCollector = _lazy_import("housekeeper.collectors.gpu_process", "GpuProcessCollector")
        gpu_proc_col = GpuProcessCollector()

    if accel["amd"] and not args.no_gpu:
        AmdGpuCollector = _lazy_import("housekeeper.collectors.amd_gpu", "AmdGpuCollector")
        amd_col = AmdGpuCollector()

    if accel["gaudi"] and not args.no_gpu:
        GaudiCollector = _lazy_import("housekeeper.collectors.gaudi", "GaudiCollector")
        gaudi_col = GaudiCollector()

    if accel.get("apple") and not args.no_gpu:
        AppleGpuCollector = _lazy_import("housekeeper.collectors.apple_gpu", "AppleGpuCollector")
        apple_col = AppleGpuCollector()

    if _has_pcie_devices():
        PcieCollector = _lazy_import("housekeeper.collectors.pcie", "PcieCollector")
        pcie_col = PcieCollector()

    if _has_net_mounts():
        NfsMountCollector = _lazy_import("housekeeper.collectors.nfs", "NfsMountCollector")
        nfs_col = NfsMountCollector()

    # 温度センサー (hwmon があれば常にロード)
    from housekeeper.collectors.temperature import TemperatureCollector
    temp_col = TemperatureCollector()

    # ベースライン取得
    cpu_col.collect()
    disk_col.collect()
    net_col.collect()
    proc_col.collect()
    kern_col.collect()
    if nfs_col:
        nfs_col.collect()
    if pcie_col:
        pcie_col.collect()
    time.sleep(0.1)

    show_pcie = True  # PCIe 表示トグル

    while True:
        # データ収集
        cpu_data = cpu_col.collect()
        mem_data, swap_data = mem_col.collect()
        disk_data = disk_col.collect()
        net_data = net_col.collect()
        proc_data = proc_col.collect()
        kern_data = kern_col.collect()

        nvidia_data = nvidia_col.collect() if nvidia_col else None
        amd_data = amd_col.collect() if amd_col else None
        gaudi_data = gaudi_col.collect() if gaudi_col else None
        apple_data = apple_col.collect() if apple_col else None
        gpu_proc_data = gpu_proc_col.collect() if gpu_proc_col else None
        pcie_data = pcie_col.collect() if pcie_col and show_pcie else None
        nfs_data = nfs_col.collect() if nfs_col else None
        temp_data = temp_col.collect()

        # NFS マウントの動的検出 (10秒ごとにチェック)
        if nfs_col is None and _has_net_mounts():
            NfsMountCollector = _lazy_import("housekeeper.collectors.nfs", "NfsMountCollector")
            nfs_col = NfsMountCollector()
            nfs_col.collect()

        # 描画
        stdscr.erase()
        renderer.render(
            stdscr,
            cpu=cpu_data,
            memory=mem_data,
            swap=swap_data,
            disks=disk_data,
            networks=net_data,
            nvidia_gpus=nvidia_data or None,
            amd_gpus=amd_data or None,
            gaudi_devices=gaudi_data or None,
            apple_gpus=apple_data or None,
            top_processes=proc_data or None,
            gpu_processes=gpu_proc_data or None,
            kernel=kern_data,
            pcie_devices=pcie_data or None,
            nfs_mounts=nfs_data or None,
            temperatures=temp_data or None,
        )
        stdscr.refresh()

        # キー入力処理
        key = stdscr.getch()
        if key == ord("q") or key == ord("Q") or key == 27:
            break
        elif key == ord("c") or key == ord("C"):
            renderer.show_per_core = not renderer.show_per_core
        elif key == ord("p") or key == ord("P"):
            show_pcie = not show_pcie
        elif key == ord("d") or key == ord("D"):
            renderer.show_raid_members = not renderer.show_raid_members
            renderer.show_bond_members = not renderer.show_bond_members
        elif key == ord("t") or key == ord("T"):
            renderer.show_temperatures = not renderer.show_temperatures
        elif key == ord("n") or key == ord("N"):
            renderer.show_networks = not renderer.show_networks
        elif key == ord("g") or key == ord("G"):
            renderer.show_gpus = not renderer.show_gpus
        elif key == ord("i") or key == ord("I"):
            renderer.show_disks = not renderer.show_disks
        elif key == ord("s") or key == ord("S"):
            renderer.show_nfs = not renderer.show_nfs
        elif key == ord("f") or key == ord("F"):
            renderer.temp_unit = "F" if renderer.temp_unit == "C" else "C"
        elif key == ord("h") or key == ord("H"):
            renderer.show_help = not renderer.show_help
        elif key == ord("+") or key == ord("="):
            args.interval = max(0.1, args.interval - 0.5)
            stdscr.timeout(int(args.interval * 1000))
        elif key == ord("-"):
            args.interval = min(10.0, args.interval + 0.5)
            stdscr.timeout(int(args.interval * 1000))


def _collect_all(args: argparse.Namespace) -> dict:
    """全コレクターを初期化・実行して結果を辞書で返す (text/gui モード用)。"""
    from housekeeper.collectors.cpu import CpuCollector
    from housekeeper.collectors.memory import MemoryCollector
    from housekeeper.collectors.disk import DiskCollector
    from housekeeper.collectors.network import NetworkCollector
    from housekeeper.collectors.process import ProcessCollector
    from housekeeper.collectors.kernel import KernelCollector

    cpu_col = CpuCollector()
    mem_col = MemoryCollector()
    disk_col = DiskCollector()
    net_col = NetworkCollector()
    proc_col = ProcessCollector(top_n=0)
    kern_col = KernelCollector()

    accel = _detect_accelerators()
    nvidia_col = amd_col = gaudi_col = apple_col = gpu_proc_col = pcie_col = nfs_col = None

    if accel["nvidia"] and not args.no_gpu:
        nvidia_col = _lazy_import("housekeeper.collectors.gpu", "GpuCollector")()
        gpu_proc_col = _lazy_import("housekeeper.collectors.gpu_process", "GpuProcessCollector")()
    if accel["amd"] and not args.no_gpu:
        amd_col = _lazy_import("housekeeper.collectors.amd_gpu", "AmdGpuCollector")()
    if accel["gaudi"] and not args.no_gpu:
        gaudi_col = _lazy_import("housekeeper.collectors.gaudi", "GaudiCollector")()
    if accel.get("apple") and not args.no_gpu:
        apple_col = _lazy_import("housekeeper.collectors.apple_gpu", "AppleGpuCollector")()
    if _has_pcie_devices():
        pcie_col = _lazy_import("housekeeper.collectors.pcie", "PcieCollector")()
    if _has_net_mounts():
        nfs_col = _lazy_import("housekeeper.collectors.nfs", "NfsMountCollector")()

    from housekeeper.collectors.temperature import TemperatureCollector
    temp_col = TemperatureCollector()

    # ベースライン
    cpu_col.collect(); disk_col.collect(); net_col.collect()
    proc_col.collect(); kern_col.collect()
    if nfs_col: nfs_col.collect()
    if pcie_col: pcie_col.collect()
    time.sleep(0.5)

    # 2回目
    cpu_data = cpu_col.collect()
    mem_data, swap_data = mem_col.collect()
    return {
        "cpu": cpu_data,
        "memory": mem_data,
        "swap": swap_data,
        "disks": disk_col.collect(),
        "networks": net_col.collect(),
        "kernel": kern_col.collect(),
        "top_processes": proc_col.collect(),
        "nvidia_gpus": nvidia_col.collect() if nvidia_col else None,
        "amd_gpus": amd_col.collect() if amd_col else None,
        "gaudi_devices": gaudi_col.collect() if gaudi_col else None,
        "apple_gpus": apple_col.collect() if apple_col else None,
        "gpu_processes": gpu_proc_col.collect() if gpu_proc_col else None,
        "pcie_devices": pcie_col.collect() if pcie_col else None,
        "nfs_mounts": nfs_col.collect() if nfs_col else None,
        "temperatures": temp_col.collect() or None,
    }


def _run_text_mode(args: argparse.Namespace) -> None:
    """テキストモードで一度出力。"""
    from housekeeper.ui.text_renderer import render_text
    data = _collect_all(args)
    data["show_per_core"] = not args.no_per_core
    print(render_text(**data))


def _run_gui(args: argparse.Namespace) -> None:
    """tkinter X11 GUI を起動。"""
    gui_mod = _lazy_import("housekeeper.ui.gui", "run_gui")
    gui_mod(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="housekeeper",
        description="xosview-like system monitor with NVIDIA/AMD/Gaudi GPU, "
                    "PCIe, NFS/SAN/NAS, per-port network support",
    )
    parser.add_argument(
        "-i", "--interval", type=float, default=1.0,
        help="Update interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--no-per-core", action="store_true",
        help="Hide per-core CPU bars (show only total)",
    )
    parser.add_argument(
        "--no-gpu", action="store_true",
        help="Disable GPU monitoring",
    )
    parser.add_argument(
        "--no-pcie", action="store_true",
        help="Disable PCIe device listing",
    )
    parser.add_argument(
        "--text", action="store_true",
        help="Text mode output (no TUI, prints to stdout)",
    )
    parser.add_argument(
        "-c", "--character", action="store_true",
        help="Character TUI mode (curses)",
    )
    parser.add_argument(
        "-x", "--gui", action="store_true",
        help="Launch X11 GUI window (tkinter) [default]",
    )
    parser.add_argument(
        "-f", "--full", action="store_true",
        help="Start in full (non-summary) mode",
    )
    parser.add_argument(
        "--profile", action="store_true",
        help="Show profiling info (frame time, collector costs)",
    )
    parser.add_argument(
        "--detect", action="store_true",
        help="Detect available hardware and exit",
    )

    args = parser.parse_args()

    if args.detect:
        accel = _detect_accelerators()
        print("Detected hardware:")
        for name, available in accel.items():
            status = "available" if available else "not found"
            print(f"  {name:8s}: {status}")
        print(f"  {'pcie':8s}: {'available' if _has_pcie_devices() else 'not found'}")
        print(f"  {'nfs/san':8s}: {'available' if _has_net_mounts() else 'not found'}")
        sys.exit(0)

    if args.text:
        _run_text_mode(args)
    elif args.character:
        try:
            curses.wrapper(lambda stdscr: _run_tui(stdscr, args))
        except KeyboardInterrupt:
            pass
    else:
        # デフォルト: GUI モード (--gui/-x も引き続き受け付ける)
        _run_gui(args)


if __name__ == "__main__":
    main()
