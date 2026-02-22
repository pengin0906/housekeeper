"""テキストモードレンダラー - ターミナルが使えない環境向け。

--text オプション時や、curses が利用不可能な場合に使用。
ANSI エスケープシーケンスで色付きバーを描画する。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from housekeeper.collectors.cpu import CpuUsage
    from housekeeper.collectors.memory import MemoryUsage, SwapUsage
    from housekeeper.collectors.disk import DiskUsage
    from housekeeper.collectors.network import NetUsage
    from housekeeper.collectors.gpu import GpuUsage
    from housekeeper.collectors.amd_gpu import AmdGpuUsage
    from housekeeper.collectors.gaudi import GaudiUsage
    from housekeeper.collectors.process import ProcessInfo
    from housekeeper.collectors.gpu_process import GpuProcessInfo
    from housekeeper.collectors.kernel import KernelInfo
    from housekeeper.collectors.pcie import PcieDeviceInfo
    from housekeeper.collectors.nfs import NfsMountUsage
    from housekeeper.collectors.temperature import TempDevice


# ANSI 色コード
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_WHITE = "\033[37m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _bar(fraction: float, width: int = 30, color: str = _GREEN) -> str:
    """ANSI カラーバーを生成。"""
    filled = int(fraction * width)
    filled = max(0, min(filled, width))
    empty = width - filled
    return f"{color}{'█' * filled}{_DIM}{'░' * empty}{_RESET}"


def _fmt_bytes_sec(bps: float) -> str:
    if bps >= 1_073_741_824:
        return f"{bps / 1_073_741_824:.1f}G/s"
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f}M/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f}K/s"
    return f"{bps:.0f}B/s"


def _fmt_mib(mib: float) -> str:
    if mib >= 1024:
        return f"{mib / 1024:.1f}G"
    return f"{mib:.0f}M"


def _fmt_rate(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return f"{v:.0f}"


def _header(title: str) -> str:
    return f"\n{_BOLD}{_WHITE}--- {title} ---{_RESET}"


def render_text(
    *,
    cpu: list[CpuUsage] | None = None,
    memory: MemoryUsage | None = None,
    swap: SwapUsage | None = None,
    disks: list[DiskUsage] | None = None,
    networks: list[NetUsage] | None = None,
    nvidia_gpus: list[GpuUsage] | None = None,
    amd_gpus: list[AmdGpuUsage] | None = None,
    gaudi_devices: list[GaudiUsage] | None = None,
    top_processes: list[ProcessInfo] | None = None,
    gpu_processes: list[GpuProcessInfo] | None = None,
    kernel: KernelInfo | None = None,
    pcie_devices: list[PcieDeviceInfo] | None = None,
    nfs_mounts: list[NfsMountUsage] | None = None,
    temperatures: list[TempDevice] | None = None,
    show_per_core: bool = False,
) -> str:
    """テキスト形式の出力を生成。"""
    lines: list[str] = []
    bar_w = 40

    lines.append(f"{_BOLD}{_WHITE}{'=' * 60}")
    lines.append(f"  housekeeper - System Monitor")
    lines.append(f"{'=' * 60}{_RESET}")

    # Kernel
    if kernel:
        k = kernel
        lines.append(_header(f"Kernel {k.kernel_version}"))
        load_frac = min(k.load_per_cpu, 1.0)
        color = _RED if load_frac > 0.8 else _GREEN
        lines.append(f"  LOAD       {_bar(load_frac, bar_w, color)} {k.load_1:.2f}/{k.load_5:.2f}/{k.load_15:.2f}")
        lines.append(f"  {_DIM}Up:{k.uptime_str}  Procs:{k.running_procs}/{k.total_procs}"
                     f"  CtxSw:{_fmt_rate(k.ctx_switches_sec)}/s"
                     f"  IRQ:{_fmt_rate(k.interrupts_sec)}/s{_RESET}")

    # CPU
    if cpu:
        lines.append(_header("CPU"))
        for c in cpu:
            is_total = c.label == "cpu"
            if not is_total and not show_per_core:
                continue
            label = "TOTAL" if is_total else c.label.upper()
            pct = c.total_pct
            color = _RED if pct > 80 else _YELLOW if pct > 50 else _GREEN
            lines.append(f"  {label:<10s} {_bar(pct / 100, bar_w, color)} {pct:5.1f}%")

    # Memory
    if memory:
        lines.append(_header("Memory"))
        m = memory
        total_g = m.total_kb / (1024 * 1024)
        used_g = m.used_kb / (1024 * 1024)
        lines.append(f"  {'MEM':<10s} {_bar(m.used_pct / 100, bar_w, _GREEN)} {used_g:.1f}/{total_g:.1f}G")

    if swap and swap.total_kb > 0:
        s = swap
        total_g = s.total_kb / (1024 * 1024)
        used_g = s.used_kb / (1024 * 1024)
        lines.append(f"  {'SWAP':<10s} {_bar(s.used_pct / 100, bar_w, _RED)} {used_g:.1f}/{total_g:.1f}G")

    # Disk
    if disks:
        lines.append(_header("Disk I/O"))
        for d in disks:
            max_bw = 1_073_741_824.0
            frac = min(d.total_bytes_sec / max_bw, 1.0)
            lines.append(f"  {d.name.upper():<10s} {_bar(frac, bar_w, _CYAN)} R:{_fmt_bytes_sec(d.read_bytes_sec)} W:{_fmt_bytes_sec(d.write_bytes_sec)}")

    # Network
    if networks:
        lines.append(_header("Network"))
        for n in networks:
            tag = n.net_type.value if hasattr(n, "net_type") else "???"
            max_bw = 125_000_000.0
            frac = min(n.total_bytes_sec / max_bw, 1.0)
            lines.append(f"  {tag:3s} {n.name:<6s} {_bar(frac, bar_w, _CYAN)} D:{_fmt_bytes_sec(n.rx_bytes_sec)} U:{_fmt_bytes_sec(n.tx_bytes_sec)}")

    # NFS
    if nfs_mounts:
        lines.append(_header("NFS/SAN/NAS"))
        for m in nfs_mounts:
            lines.append(f"  {m.type_label:3s} {m.mount_point:<20s} R:{_fmt_bytes_sec(m.read_bytes_sec)} W:{_fmt_bytes_sec(m.write_bytes_sec)}")

    # Temperature (hwmon + GPU)
    if temperatures or nvidia_gpus or amd_gpus or gaudi_devices:
        lines.append(_header("Temperature"))
        if temperatures:
            for dev in temperatures:
                temp = dev.primary_temp_c
                crit = dev.primary_crit_c or 100.0
                frac = min(temp / crit, 1.0) if crit > 0 else min(temp / 100.0, 1.0)
                color = _RED if temp > crit * 0.8 else _GREEN
                val = f"{temp:.0f}C"
                if dev.primary_crit_c > 0:
                    val += f" (crit={crit:.0f}C)"
                lines.append(f"  {dev.display_name:<20s} {_bar(frac, bar_w, color)} {val}")
        if nvidia_gpus:
            for g in nvidia_gpus:
                frac = min(g.temperature_c / 100.0, 1.0)
                color = _RED if g.temperature_c > 80 else _GREEN
                lines.append(f"  {'GPU' + str(g.index):<20s} {_bar(frac, bar_w, color)} {g.temperature_c:.0f}C")
        if amd_gpus:
            for g in amd_gpus:
                if g.temperature_c > 0:
                    frac = min(g.temperature_c / 100.0, 1.0)
                    color = _RED if g.temperature_c > 80 else _GREEN
                    lines.append(f"  {'AMD' + str(g.index):<20s} {_bar(frac, bar_w, color)} {g.temperature_c:.0f}C")
        if gaudi_devices:
            for d in gaudi_devices:
                if d.temperature_c > 0:
                    frac = min(d.temperature_c / 100.0, 1.0)
                    color = _RED if d.temperature_c > 80 else _GREEN
                    lines.append(f"  {'HL' + str(d.index):<20s} {_bar(frac, bar_w, color)} {d.temperature_c:.0f}C")

    # PCIe
    if pcie_devices:
        lines.append(_header("PCIe Devices"))
        for d in pcie_devices:
            link = f"{d.gen_name:4s} x{d.current_width:<2d} {d.current_bandwidth_gbs:5.1f} GB/s"
            if d.io_label:
                io_info = f"  R:{_fmt_bytes_sec(d.io_read_bytes_sec)} W:{_fmt_bytes_sec(d.io_write_bytes_sec)}"
                lines.append(f"  {d.short_name[:28]:<28s} {link}  [{d.io_label}]{io_info}")
            else:
                lines.append(f"  {d.short_name[:28]:<28s} {link}")

    # NVIDIA GPU
    if nvidia_gpus:
        lines.append(_header("NVIDIA GPU"))
        for g in nvidia_gpus:
            lines.append(f"  GPU{g.index} {g.short_name}")
            color = _RED if g.gpu_util_pct > 80 else _GREEN
            lines.append(f"    UTIL     {_bar(g.gpu_util_pct / 100, bar_w, color)} {g.gpu_util_pct:.0f}%")
            lines.append(f"    VRAM     {_bar(g.mem_used_pct / 100, bar_w, _YELLOW)} {_fmt_mib(g.mem_used_mib)}/{_fmt_mib(g.mem_total_mib)}")
            lines.append(f"    TEMP     {_bar(g.temperature_c / 100, bar_w, _RED)} {g.temperature_c:.0f}C")
            lines.append(f"    POWER    {_bar(g.power_pct / 100, bar_w, _MAGENTA)} {g.power_draw_w:.0f}/{g.power_limit_w:.0f}W")

    # AMD GPU
    if amd_gpus:
        lines.append(_header("AMD GPU (ROCm)"))
        for g in amd_gpus:
            lines.append(f"  GPU{g.index} {g.short_name}")
            lines.append(f"    UTIL     {_bar(g.gpu_util_pct / 100, bar_w, _GREEN)} {g.gpu_util_pct:.0f}%")
            if g.mem_total_mib > 0:
                lines.append(f"    VRAM     {_bar(g.mem_used_pct / 100, bar_w, _YELLOW)} {_fmt_mib(g.mem_used_mib)}/{_fmt_mib(g.mem_total_mib)}")

    # Gaudi
    if gaudi_devices:
        lines.append(_header("Intel Gaudi"))
        for d in gaudi_devices:
            lines.append(f"  HL{d.index} {d.short_name}")
            lines.append(f"    AIP      {_bar(d.aip_util_pct / 100, bar_w, _GREEN)} {d.aip_util_pct:.0f}%")
            if d.mem_total_mib > 0:
                lines.append(f"    HBM      {_bar(d.mem_used_pct / 100, bar_w, _YELLOW)} {_fmt_mib(d.mem_used_mib)}/{_fmt_mib(d.mem_total_mib)}")

    # GPU Processes
    if gpu_processes:
        lines.append(_header("GPU Processes"))
        for p in gpu_processes:
            lines.append(f"  GPU{p.gpu_index}  PID:{p.pid:>7d}  {p.name:<18s}  VRAM:{p.gpu_mem_mib:7.0f} MiB")

    # Top Processes
    if top_processes:
        lines.append(_header("Top Processes"))
        for p in top_processes:
            color = _RED if p.cpu_pct > 100 else _YELLOW if p.cpu_pct > 50 else ""
            lines.append(f"  {color}PID:{p.pid:>7d}  {p.name:<20s}  CPU:{p.cpu_pct:5.1f}%  MEM:{p.mem_rss_mib:7.1f}M{_RESET if color else ''}")

    return "\n".join(lines)
