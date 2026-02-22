"""メイン TUI レンダラー - xosview 風のバーメーターを curses で描画。

全コレクターの結果を受け取り、画面に描画する。
セクション:
  1. Kernel (version, load average, uptime, context switches)
  2. CPU (全体 + per-core)
  3. Memory / Swap
  4. Disk I/O
  5. Network I/O (WAN/LAN/VIR 分類)
  6. NFS/SAN/NAS マウント
  7. PCIe デバイス
  8. NVIDIA GPU (あれば)
  9. AMD GPU (あれば)
  10. Intel Gaudi (あれば)
  11. GPU Processes
  12. Top Processes (CPU/MEM)
"""

from __future__ import annotations

import curses
from typing import TYPE_CHECKING

from housekeeper.ui.bar import BarSegment, draw_bar, draw_section_header
from housekeeper.ui.colors import (
    PAIR_CACHE, PAIR_GPU_FAN, PAIR_GPU_MEM, PAIR_GPU_POWER,
    PAIR_GPU_TEMP, PAIR_GPU_UTIL, PAIR_HEADER, PAIR_IDLE, PAIR_IOWAIT,
    PAIR_IRQ, PAIR_LABEL, PAIR_NET_RX, PAIR_NET_TX, PAIR_NICE,
    PAIR_STEAL, PAIR_SWAP, PAIR_SYSTEM, PAIR_USER, PAIR_GPU_ENC,
)

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


def _fmt_bytes_sec(bps: float) -> str:
    """バイト/秒を人間が読める形式に。"""
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


class Renderer:
    """画面レンダラー。"""

    def __init__(self, show_per_core: bool = True) -> None:
        self.show_per_core = show_per_core

    def render(
        self,
        win: curses.window,
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
    ) -> None:
        max_y, max_x = win.getmaxyx()
        width = max_x - 2
        label_w = 10
        val_w = 10
        y = 0
        x = 1

        # タイトルバー
        title = " housekeeper - System Monitor "
        try:
            pad = "=" * max(0, (width - len(title)) // 2)
            header = f"{pad}{title}{pad}"
            if len(header) < width:
                header += "=" * (width - len(header))
            win.addnstr(y, x, header[:width], width,
                         curses.color_pair(PAIR_HEADER) | curses.A_BOLD)
        except curses.error:
            pass
        y += 1

        # Kernel
        if kernel:
            y = self._render_kernel(win, y, x, width, label_w, val_w, kernel)

        # CPU
        if cpu:
            y = self._render_cpu(win, y, x, width, label_w, val_w, cpu)

        # Memory
        if memory:
            y = self._render_memory(win, y, x, width, label_w, val_w, memory)

        # Swap
        if swap and swap.total_kb > 0:
            y = self._render_swap(win, y, x, width, label_w, val_w, swap)

        # Disk
        if disks:
            y = self._render_disks(win, y, x, width, label_w, val_w, disks)

        # Network
        if networks:
            y = self._render_networks(win, y, x, width, label_w, val_w, networks)

        # NFS/SAN/NAS
        if nfs_mounts:
            y = self._render_nfs(win, y, x, width, label_w, val_w, nfs_mounts)

        # Temperature (hwmon + GPU)
        if temperatures or nvidia_gpus or amd_gpus or gaudi_devices:
            y = self._render_temperatures(
                win, y, x, width, label_w, val_w,
                devices=temperatures or [],
                nvidia_gpus=nvidia_gpus,
                amd_gpus=amd_gpus,
                gaudi_devices=gaudi_devices,
            )

        # PCIe
        if pcie_devices:
            y = self._render_pcie(win, y, x, width, label_w, val_w, pcie_devices)

        # NVIDIA GPU
        if nvidia_gpus:
            y = self._render_nvidia(win, y, x, width, label_w, val_w, nvidia_gpus)

        # AMD GPU
        if amd_gpus:
            y = self._render_amd(win, y, x, width, label_w, val_w, amd_gpus)

        # Intel Gaudi
        if gaudi_devices:
            y = self._render_gaudi(win, y, x, width, label_w, val_w, gaudi_devices)

        # GPU Processes
        if gpu_processes:
            y = self._render_gpu_processes(win, y, x, width, gpu_processes)

        # Top Processes
        if top_processes:
            y = self._render_processes(win, y, x, width, top_processes)

        # フッター
        if y < max_y - 1:
            footer = " q:quit  c:toggle cores  p:toggle pcie  +/-:interval "
            try:
                win.addnstr(max_y - 1, x, footer[:width], width,
                             curses.color_pair(PAIR_HEADER) | curses.A_DIM)
            except curses.error:
                pass

    # ─── Kernel ─────────────────────────────────────────────

    def _render_kernel(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, k: KernelInfo,
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, f"Kernel {k.kernel_version}", PAIR_HEADER)
        y += 1

        if y >= max_y - 1:
            return y

        # Load Average bar
        load_frac = min(k.load_per_cpu, 1.0)
        color = PAIR_SYSTEM if load_frac > 0.8 else PAIR_USER
        draw_bar(win, y, x, width,
                 [BarSegment(load_frac, color)],
                 label="LOAD", label_width=label_w,
                 value_text=f"{k.load_1:.2f}/{k.load_5:.2f}/{k.load_15:.2f}",
                 value_width=val_w + 6, label_color=PAIR_LABEL)
        y += 1

        if y >= max_y - 1:
            return y

        info = (f" Up:{k.uptime_str}  Procs:{k.running_procs}/{k.total_procs}"
                f"  CtxSw:{_fmt_rate(k.ctx_switches_sec)}/s"
                f"  IRQ:{_fmt_rate(k.interrupts_sec)}/s")
        try:
            win.addnstr(y, x, info[:width], width,
                         curses.color_pair(PAIR_LABEL) | curses.A_DIM)
        except curses.error:
            pass
        y += 1
        return y

    # ─── CPU ────────────────────────────────────────────────

    def _render_cpu(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, cpu: list[CpuUsage],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "CPU", PAIR_HEADER)
        y += 1

        for usage in cpu:
            if y >= max_y - 1:
                break
            is_total = usage.label == "cpu"
            if not is_total and not self.show_per_core:
                continue

            label = "TOTAL" if is_total else usage.label.upper()
            segments = [
                BarSegment(usage.user_pct / 100, PAIR_USER),
                BarSegment(usage.nice_pct / 100, PAIR_NICE),
                BarSegment(usage.system_pct / 100, PAIR_SYSTEM),
                BarSegment(usage.iowait_pct / 100, PAIR_IOWAIT),
                BarSegment(usage.irq_pct / 100, PAIR_IRQ),
                BarSegment(usage.steal_pct / 100, PAIR_STEAL),
            ]
            draw_bar(win, y, x, width, segments,
                     label=label, label_width=label_w,
                     value_text=f"{usage.total_pct:5.1f}%",
                     value_width=val_w, label_color=PAIR_LABEL)
            y += 1

        return y

    # ─── Memory ─────────────────────────────────────────────

    def _render_memory(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, mem: MemoryUsage,
    ) -> int:
        draw_section_header(win, y, x, width, "Memory", PAIR_HEADER)
        y += 1

        segments = [
            BarSegment(mem.used_pct / 100, PAIR_USER),
            BarSegment(mem.buffers_pct / 100, PAIR_IRQ),
            BarSegment(mem.cached_pct / 100, PAIR_CACHE),
        ]
        total_gib = mem.total_kb / (1024 * 1024)
        used_gib = mem.used_kb / (1024 * 1024)
        draw_bar(win, y, x, width, segments,
                 label="MEM", label_width=label_w,
                 value_text=f"{used_gib:.1f}/{total_gib:.1f}G",
                 value_width=val_w + 2, label_color=PAIR_LABEL)
        return y + 1

    def _render_swap(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, swap: SwapUsage,
    ) -> int:
        segments = [BarSegment(swap.used_pct / 100, PAIR_SWAP)]
        total_gib = swap.total_kb / (1024 * 1024)
        used_gib = swap.used_kb / (1024 * 1024)
        draw_bar(win, y, x, width, segments,
                 label="SWAP", label_width=label_w,
                 value_text=f"{used_gib:.1f}/{total_gib:.1f}G",
                 value_width=val_w + 2, label_color=PAIR_LABEL)
        return y + 1

    # ─── Disk I/O ───────────────────────────────────────────

    def _render_disks(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, disks: list[DiskUsage],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "Disk I/O", PAIR_HEADER)
        y += 1

        for d in disks:
            if y >= max_y - 1:
                break
            max_bw = 1_073_741_824.0  # 1 GB/s
            rd_frac = min(d.read_bytes_sec / max_bw, 0.5)
            wr_frac = min(d.write_bytes_sec / max_bw, 0.5)
            segments = [
                BarSegment(rd_frac, PAIR_CACHE),
                BarSegment(wr_frac, PAIR_IOWAIT),
            ]
            val = f"R:{_fmt_bytes_sec(d.read_bytes_sec)}"
            draw_bar(win, y, x, width, segments,
                     label=d.name.upper()[:label_w], label_width=label_w,
                     value_text=val, value_width=val_w + 2,
                     label_color=PAIR_LABEL)
            y += 1

        return y

    # ─── Network ────────────────────────────────────────────

    def _render_networks(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, networks: list[NetUsage],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "Network", PAIR_HEADER)
        y += 1

        for n in networks:
            if y >= max_y - 1:
                break
            max_bw = 125_000_000.0  # 1 Gbps
            rx_frac = min(n.rx_bytes_sec / max_bw, 0.5)
            tx_frac = min(n.tx_bytes_sec / max_bw, 0.5)
            segments = [
                BarSegment(rx_frac, PAIR_NET_RX),
                BarSegment(tx_frac, PAIR_NET_TX),
            ]
            tag = n.net_type.value if hasattr(n, "net_type") else ""
            label = f"{tag:3s} {n.name}"[:label_w]
            val = f"D:{_fmt_bytes_sec(n.rx_bytes_sec)} U:{_fmt_bytes_sec(n.tx_bytes_sec)}"
            draw_bar(win, y, x, width, segments,
                     label=label, label_width=label_w,
                     value_text=val, value_width=val_w + 10,
                     label_color=PAIR_LABEL)
            y += 1

        return y

    # ─── NFS/SAN/NAS ────────────────────────────────────────

    def _render_nfs(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, mounts: list[NfsMountUsage],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "NFS/SAN/NAS", PAIR_HEADER)
        y += 1

        for m in mounts:
            if y >= max_y - 1:
                break
            max_bw = 125_000_000.0  # 1 Gbps
            rd_frac = min(m.read_bytes_sec / max_bw, 0.5)
            wr_frac = min(m.write_bytes_sec / max_bw, 0.5)
            segments = [
                BarSegment(rd_frac, PAIR_NET_RX),
                BarSegment(wr_frac, PAIR_NET_TX),
            ]
            label = f"{m.type_label:3s} {m.mount_point}"[:label_w]
            val = f"R:{_fmt_bytes_sec(m.read_bytes_sec)}"
            draw_bar(win, y, x, width, segments,
                     label=label, label_width=label_w,
                     value_text=val, value_width=val_w + 2,
                     label_color=PAIR_LABEL)
            y += 1

        return y

    # ─── Temperature ─────────────────────────────────────────

    def _render_temperatures(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int,
        devices: list[TempDevice],
        nvidia_gpus: list[GpuUsage] | None = None,
        amd_gpus: list[AmdGpuUsage] | None = None,
        gaudi_devices: list[GaudiUsage] | None = None,
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "Temperature", PAIR_HEADER)
        y += 1

        # hwmon センサー
        for dev in devices:
            if y >= max_y - 1:
                break
            temp = dev.primary_temp_c
            crit = dev.primary_crit_c or 100.0
            frac = min(temp / crit, 1.0) if crit > 0 else min(temp / 100.0, 1.0)
            color = PAIR_GPU_TEMP if temp > crit * 0.8 else PAIR_GPU_UTIL

            label = dev.display_name[:label_w]
            val = f"{temp:.0f}C"
            if dev.primary_crit_c > 0:
                val += f"/{crit:.0f}C"

            draw_bar(win, y, x, width,
                     [BarSegment(frac, color)],
                     label=label, label_width=label_w,
                     value_text=val, value_width=val_w + 4,
                     label_color=PAIR_LABEL)
            y += 1

        # NVIDIA GPU 温度
        if nvidia_gpus:
            for g in nvidia_gpus:
                if y >= max_y - 1:
                    break
                temp = g.temperature_c
                frac = min(temp / 100.0, 1.0)
                color = PAIR_GPU_TEMP if temp > 80 else PAIR_GPU_UTIL
                draw_bar(win, y, x, width,
                         [BarSegment(frac, color)],
                         label=f"GPU{g.index}", label_width=label_w,
                         value_text=f"{temp:.0f}C",
                         value_width=val_w + 4, label_color=PAIR_LABEL)
                y += 1

        # AMD GPU 温度
        if amd_gpus:
            for g in amd_gpus:
                if y >= max_y - 1 or g.temperature_c <= 0:
                    break
                temp = g.temperature_c
                frac = min(temp / 100.0, 1.0)
                color = PAIR_GPU_TEMP if temp > 80 else PAIR_GPU_UTIL
                draw_bar(win, y, x, width,
                         [BarSegment(frac, color)],
                         label=f"AMD{g.index}", label_width=label_w,
                         value_text=f"{temp:.0f}C",
                         value_width=val_w + 4, label_color=PAIR_LABEL)
                y += 1

        # Gaudi 温度
        if gaudi_devices:
            for d in gaudi_devices:
                if y >= max_y - 1 or d.temperature_c <= 0:
                    break
                temp = d.temperature_c
                frac = min(temp / 100.0, 1.0)
                color = PAIR_GPU_TEMP if temp > 80 else PAIR_GPU_UTIL
                draw_bar(win, y, x, width,
                         [BarSegment(frac, color)],
                         label=f"HL{d.index}", label_width=label_w,
                         value_text=f"{temp:.0f}C",
                         value_width=val_w + 4, label_color=PAIR_LABEL)
                y += 1

        return y

    # ─── PCIe ───────────────────────────────────────────────

    def _render_pcie(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, devices: list[PcieDeviceInfo],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "PCIe Devices", PAIR_HEADER)
        y += 1

        for dev in devices:
            if y >= max_y - 1:
                break

            # I/O スループットバー (理論帯域比)
            io_util = dev.io_utilization
            name = dev.short_name[:20]
            link = f"{dev.gen_name} x{dev.current_width}"

            if dev.io_label:
                # I/O データあり: バー表示
                io_total = dev.io_read_bytes_sec + dev.io_write_bytes_sec
                segments = [
                    BarSegment(min(dev.io_read_bytes_sec / max(dev.current_bandwidth_gbs * 1_073_741_824, 1), 0.5), PAIR_CACHE),
                    BarSegment(min(dev.io_write_bytes_sec / max(dev.current_bandwidth_gbs * 1_073_741_824, 1), 0.5), PAIR_IOWAIT),
                ]
                val = f"{link} R:{_fmt_bytes_sec(dev.io_read_bytes_sec)} W:{_fmt_bytes_sec(dev.io_write_bytes_sec)}"
                draw_bar(win, y, x, width, segments,
                         label=name, label_width=label_w + 10,
                         value_text=val, value_width=val_w + 20,
                         label_color=PAIR_LABEL)
            else:
                # I/O データなし: リンク情報のみ
                link_info = f"{link} {dev.current_bandwidth_gbs:.1f}GB/s"
                try:
                    win.addnstr(y, x, f" {name}", min(width, 30),
                                 curses.color_pair(PAIR_LABEL))
                    win.addnstr(y, x + 30, link_info, min(width - 30, 30),
                                 curses.color_pair(PAIR_CACHE) | curses.A_DIM)
                except curses.error:
                    pass
            y += 1

        return y

    # ─── NVIDIA GPU ─────────────────────────────────────────

    def _render_nvidia(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, gpus: list[GpuUsage],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "NVIDIA GPU", PAIR_HEADER)
        y += 1

        for gpu in gpus:
            if y >= max_y - 4:
                break

            name = f"GPU{gpu.index}"

            draw_bar(win, y, x, width,
                     [BarSegment(gpu.gpu_util_pct / 100, PAIR_GPU_UTIL)],
                     label=f"{name} UTIL", label_width=label_w,
                     value_text=f"{gpu.gpu_util_pct:.0f}%",
                     value_width=val_w, label_color=PAIR_LABEL)
            y += 1

            if y < max_y - 1:
                draw_bar(win, y, x, width,
                         [BarSegment(gpu.mem_used_pct / 100, PAIR_GPU_MEM)],
                         label=f"{name} VRAM", label_width=label_w,
                         value_text=f"{_fmt_mib(gpu.mem_used_mib)}/{_fmt_mib(gpu.mem_total_mib)}",
                         value_width=val_w + 2, label_color=PAIR_LABEL)
                y += 1

            if y < max_y - 1:
                temp_frac = min(gpu.temperature_c / 100.0, 1.0)
                draw_bar(win, y, x, width,
                         [BarSegment(temp_frac, PAIR_GPU_TEMP)],
                         label=f"{name} TEMP", label_width=label_w,
                         value_text=f"{gpu.temperature_c:.0f}C",
                         value_width=val_w, label_color=PAIR_LABEL)
                y += 1

            if y < max_y - 1:
                draw_bar(win, y, x, width,
                         [BarSegment(gpu.power_pct / 100, PAIR_GPU_POWER)],
                         label=f"{name} PWR", label_width=label_w,
                         value_text=f"{gpu.power_draw_w:.0f}/{gpu.power_limit_w:.0f}W",
                         value_width=val_w + 2, label_color=PAIR_LABEL)
                y += 1

        return y

    # ─── AMD GPU ────────────────────────────────────────────

    def _render_amd(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, gpus: list[AmdGpuUsage],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "AMD GPU (ROCm)", PAIR_HEADER)
        y += 1

        for gpu in gpus:
            if y >= max_y - 4:
                break
            name = f"GPU{gpu.index}"

            draw_bar(win, y, x, width,
                     [BarSegment(gpu.gpu_util_pct / 100, PAIR_GPU_UTIL)],
                     label=f"{name} UTIL", label_width=label_w,
                     value_text=f"{gpu.gpu_util_pct:.0f}%",
                     value_width=val_w, label_color=PAIR_LABEL)
            y += 1

            if y < max_y - 1 and gpu.mem_total_mib > 0:
                draw_bar(win, y, x, width,
                         [BarSegment(gpu.mem_used_pct / 100, PAIR_GPU_MEM)],
                         label=f"{name} VRAM", label_width=label_w,
                         value_text=f"{_fmt_mib(gpu.mem_used_mib)}/{_fmt_mib(gpu.mem_total_mib)}",
                         value_width=val_w + 2, label_color=PAIR_LABEL)
                y += 1

            if y < max_y - 1 and gpu.temperature_c > 0:
                temp_frac = min(gpu.temperature_c / 100.0, 1.0)
                draw_bar(win, y, x, width,
                         [BarSegment(temp_frac, PAIR_GPU_TEMP)],
                         label=f"{name} TEMP", label_width=label_w,
                         value_text=f"{gpu.temperature_c:.0f}C",
                         value_width=val_w, label_color=PAIR_LABEL)
                y += 1

            if y < max_y - 1 and gpu.power_draw_w > 0:
                pwr_frac = gpu.power_pct / 100 if gpu.power_limit_w else min(gpu.power_draw_w / 500, 1.0)
                label_val = (f"{gpu.power_draw_w:.0f}/{gpu.power_limit_w:.0f}W"
                             if gpu.power_limit_w else f"{gpu.power_draw_w:.0f}W")
                draw_bar(win, y, x, width,
                         [BarSegment(pwr_frac, PAIR_GPU_POWER)],
                         label=f"{name} PWR", label_width=label_w,
                         value_text=label_val,
                         value_width=val_w + 2, label_color=PAIR_LABEL)
                y += 1

        return y

    # ─── Intel Gaudi ────────────────────────────────────────

    def _render_gaudi(
        self, win: curses.window, y: int, x: int, width: int,
        label_w: int, val_w: int, devices: list[GaudiUsage],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "Intel Gaudi", PAIR_HEADER)
        y += 1

        for dev in devices:
            if y >= max_y - 3:
                break
            name = f"HL{dev.index}"

            draw_bar(win, y, x, width,
                     [BarSegment(dev.aip_util_pct / 100, PAIR_GPU_UTIL)],
                     label=f"{name} AIP", label_width=label_w,
                     value_text=f"{dev.aip_util_pct:.0f}%",
                     value_width=val_w, label_color=PAIR_LABEL)
            y += 1

            if y < max_y - 1 and dev.mem_total_mib > 0:
                draw_bar(win, y, x, width,
                         [BarSegment(dev.mem_used_pct / 100, PAIR_GPU_MEM)],
                         label=f"{name} HBM", label_width=label_w,
                         value_text=f"{_fmt_mib(dev.mem_used_mib)}/{_fmt_mib(dev.mem_total_mib)}",
                         value_width=val_w + 2, label_color=PAIR_LABEL)
                y += 1

            if y < max_y - 1 and dev.temperature_c > 0:
                temp_frac = min(dev.temperature_c / 100.0, 1.0)
                draw_bar(win, y, x, width,
                         [BarSegment(temp_frac, PAIR_GPU_TEMP)],
                         label=f"{name} TEMP", label_width=label_w,
                         value_text=f"{dev.temperature_c:.0f}C",
                         value_width=val_w, label_color=PAIR_LABEL)
                y += 1

            if y < max_y - 1 and dev.power_draw_w > 0:
                pwr_frac = min(dev.power_draw_w / 600, 1.0)
                draw_bar(win, y, x, width,
                         [BarSegment(pwr_frac, PAIR_GPU_POWER)],
                         label=f"{name} PWR", label_width=label_w,
                         value_text=f"{dev.power_draw_w:.0f}W",
                         value_width=val_w + 2, label_color=PAIR_LABEL)
                y += 1

        return y

    # ─── Processes ──────────────────────────────────────────

    def _render_processes(
        self, win: curses.window, y: int, x: int, width: int,
        procs: list[ProcessInfo],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "Top Processes", PAIR_HEADER)
        y += 1

        for p in procs:
            if y >= max_y - 1:
                break
            line = f" {p.pid:>7d}  {p.name:<20s} CPU:{p.cpu_pct:5.1f}%  MEM:{p.mem_rss_mib:7.1f}M"
            color = PAIR_USER if p.cpu_pct > 50 else PAIR_LABEL
            try:
                win.addnstr(y, x, line[:width], width,
                             curses.color_pair(color))
            except curses.error:
                pass
            y += 1

        return y

    def _render_gpu_processes(
        self, win: curses.window, y: int, x: int, width: int,
        procs: list[GpuProcessInfo],
    ) -> int:
        max_y, _ = win.getmaxyx()
        draw_section_header(win, y, x, width, "GPU Processes", PAIR_HEADER)
        y += 1

        for p in procs:
            if y >= max_y - 1:
                break
            line = f" GPU{p.gpu_index} PID:{p.pid:>7d}  {p.name:<18s} VRAM:{p.gpu_mem_mib:7.0f}MiB"
            try:
                win.addnstr(y, x, line[:width], width,
                             curses.color_pair(PAIR_GPU_MEM))
            except curses.error:
                pass
            y += 1

        return y
