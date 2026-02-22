"""tkinter ベースの X11 GUI - EVA風システムモニター。

CLIから `housekeeper` (デフォルト) または `housekeeper -x` で起動する。
tkinter は Python 標準ライブラリなので追加依存なし。

各セクションヘッダーをクリックすると展開/折りたたみできる。
RAID / Bond 行をクリックするとメンバーを展開/折りたたみできる。
折りたたみ時はサマリー行(合計のみ)を表示する。
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import tkinter as tk
from pathlib import Path
from typing import Any


# ─── EVA カラーパレット ────────────────────────────────────
COLORS = {
    # 基本
    "bg": "#0a0a14",
    "fg": "#ff6600",
    "fg_data": "#e0e0e0",
    "fg_sub": "#cc8800",
    # ヘッダー
    "header": "#1a0a2e",
    "header_line": "#ff6600",
    # バー
    "bar_bg": "#1a1a2e",
    "bar_border": "#333344",
    # テキスト
    "text_dim": "#666655",
    "text_warn": "#ff3333",
    # データ色
    "user": "#00cc66",
    "nice": "#cccc00",
    "system": "#cc3333",
    "iowait": "#cc66cc",
    "irq": "#3366cc",
    "idle": "#444444",
    "cache": "#00cccc",
    "swap": "#cc3333",
    "gpu_util": "#00cc66",
    "gpu_mem": "#cccc00",
    "gpu_temp": "#cc3333",
    "gpu_power": "#cc66cc",
    "gpu_fan": "#00cccc",
    "net_rx": "#00cccc",
    "net_tx": "#00cc66",
    "pcie": "#6699cc",
    "warn": "#ff6600",
}


def _lazy_import(module_path: str, class_name: str):
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


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


class HousekeeperGui:
    """EVA風 GUI システムモニター。"""

    SECTIONS = {
        "kernel": True,
        "cpu": False,
        "memory": True,
        "temp": True,
        "disk": False,
        "network": True,
        "nfs": True,
        "pcie": False,
        "nvidia": True,
        "amd": True,
        "gaudi": True,
        "gpu_proc": False,
        "proc": False,
        # RAID / Bond メンバー展開
        "raid_members": False,
        "bond_members": False,
    }

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.interval_ms = int(args.interval * 1000)
        self.expanded: dict[str, bool] = dict(self.SECTIONS)

        # クリック領域: セクションヘッダー + トグル行
        self._header_zones: list[tuple[int, int, str]] = []
        self._toggle_zones: list[tuple[int, int, str]] = []

        # ウィンドウ設定
        self.root = tk.Tk()
        self.root.title("housekeeper - System Monitor")
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry("850x900")
        self.root.minsize(600, 400)

        # Canvas (スクロール対応)
        self.frame = tk.Frame(self.root, bg=COLORS["bg"])
        self.frame.pack(fill=tk.BOTH, expand=True)

        self.scrollbar = tk.Scrollbar(self.frame, orient=tk.VERTICAL)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.canvas = tk.Canvas(
            self.frame, bg=COLORS["bg"], highlightthickness=0,
            yscrollcommand=self.scrollbar.set,
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.config(command=self.canvas.yview)

        # イベント
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        self.root.bind("<q>", lambda e: self.root.quit())
        self.root.bind("<Escape>", lambda e: self.root.quit())
        self.root.bind("<plus>", lambda e: self._change_interval(-500))
        self.root.bind("<minus>", lambda e: self._change_interval(500))

        self._init_collectors()

    def _init_collectors(self) -> None:
        from housekeeper.collectors.cpu import CpuCollector
        from housekeeper.collectors.memory import MemoryCollector
        from housekeeper.collectors.disk import DiskCollector
        from housekeeper.collectors.network import NetworkCollector
        from housekeeper.collectors.process import ProcessCollector
        from housekeeper.collectors.kernel import KernelCollector

        self.cpu_col = CpuCollector()
        self.mem_col = MemoryCollector()
        self.disk_col = DiskCollector()
        self.net_col = NetworkCollector()
        self.proc_col = ProcessCollector(top_n=8)
        self.kern_col = KernelCollector()

        accel = {
            "nvidia": bool(shutil.which("nvidia-smi")),
            "amd": bool(shutil.which("rocm-smi")),
            "gaudi": bool(shutil.which("hl-smi")),
        }

        from housekeeper.collectors.temperature import TemperatureCollector
        self.temp_col = TemperatureCollector()

        self.nvidia_col = self.amd_col = self.gaudi_col = None
        self.gpu_proc_col = self.pcie_col = self.nfs_col = None

        if accel["nvidia"] and not self.args.no_gpu:
            self.nvidia_col = _lazy_import("housekeeper.collectors.gpu", "GpuCollector")()
            self.gpu_proc_col = _lazy_import("housekeeper.collectors.gpu_process", "GpuProcessCollector")()
        if accel["amd"] and not self.args.no_gpu:
            self.amd_col = _lazy_import("housekeeper.collectors.amd_gpu", "AmdGpuCollector")()
        if accel["gaudi"] and not self.args.no_gpu:
            self.gaudi_col = _lazy_import("housekeeper.collectors.gaudi", "GaudiCollector")()
        if Path("/sys/bus/pci/devices").exists():
            self.pcie_col = _lazy_import("housekeeper.collectors.pcie", "PcieCollector")()

        net_fs = {"nfs", "nfs4", "nfs3", "cifs", "smbfs", "glusterfs", "ceph", "lustre"}
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] in net_fs:
                        self.nfs_col = _lazy_import("housekeeper.collectors.nfs", "NfsMountCollector")()
                        break
        except OSError:
            pass

        # ベースライン
        self.cpu_col.collect()
        self.disk_col.collect()
        self.net_col.collect()
        self.proc_col.collect()
        self.kern_col.collect()
        if self.nfs_col:
            self.nfs_col.collect()
        if self.pcie_col:
            self.pcie_col.collect()

    # ─── イベント ───────────────────────────────────────────

    def _on_click(self, event: Any) -> None:
        """Canvas クリック: ヘッダー行 or トグル行で展開/折りたたみ。"""
        cy = self.canvas.canvasy(event.y)
        # トグル行を先にチェック (ヘッダー内にある場合があるため)
        for y1, y2, key in self._toggle_zones:
            if y1 <= cy <= y2:
                self.expanded[key] = not self.expanded[key]
                return
        for y1, y2, key in self._header_zones:
            if y1 <= cy <= y2:
                self.expanded[key] = not self.expanded[key]
                return

    def _change_interval(self, delta_ms: int) -> None:
        self.interval_ms = max(100, min(10000, self.interval_ms + delta_ms))

    # ─── 描画ヘルパー ──────────────────────────────────────

    def _draw_section_header(self, y: int, key: str, title: str,
                             summary: str = "") -> int:
        """EVA風セクションヘッダー: オレンジのライン + タイトル。"""
        c = self.canvas
        c_width = c.winfo_width() or 850
        expanded = self.expanded.get(key, True)
        icon = "▼" if expanded else "▶"
        h = 24

        # 背景
        c.create_rectangle(0, y, c_width, y + h,
                           fill=COLORS["header"], outline="")
        # 上ライン
        c.create_line(0, y, c_width, y, fill=COLORS["header_line"], width=1)

        # アイコン + ─── + タイトル + ───
        header_text = f"{icon} ── {title} "
        c.create_text(10, y + h // 2, anchor="w", text=header_text,
                      fill=COLORS["fg"], font=("monospace", 11, "bold"))

        # タイトル右側のライン
        text_end = 10 + len(header_text) * 8  # 概算
        if text_end < c_width - 20:
            line_str = "─" * max(0, (c_width - text_end - 20) // 8)
            c.create_text(text_end, y + h // 2, anchor="w", text=line_str,
                          fill=COLORS["fg_sub"], font=("monospace", 11))

        # 折りたたみ時はサマリーを右側に
        if not expanded and summary:
            c.create_text(c_width - 15, y + h // 2, anchor="e", text=summary,
                          fill=COLORS["fg_sub"], font=("monospace", 9))

        # 下ライン
        c.create_line(0, y + h - 1, c_width, y + h - 1,
                      fill=COLORS["bar_border"], width=1)

        self._header_zones.append((y, y + h, key))
        return y + h + 2

    def _draw_bar(self, y: int, label: str, segments: list[tuple[float, str]],
                  value: str, label_width: int = 90) -> int:
        """EVA風バーメーター。"""
        c = self.canvas
        c_width = c.winfo_width() or 850
        lw = label_width
        bw = max(c_width - lw - 190, 100)
        h = 16
        x = 10

        # Label (オレンジ)
        c.create_text(x, y + h // 2, anchor="w", text=label,
                      fill=COLORS["fg"], font=("monospace", 10, "bold"))
        x += lw

        # Bar 背景 + ボーダー
        c.create_rectangle(x, y + 1, x + bw, y + h - 1,
                           fill=COLORS["bar_bg"], outline=COLORS["bar_border"])

        # セグメント
        bx = x
        for frac, color in segments:
            if frac <= 0:
                continue
            sw = frac * bw
            c.create_rectangle(bx, y + 2, bx + sw, y + h - 2,
                               fill=color, outline="")
            bx += sw

        # 値テキスト
        c.create_text(x + bw + 10, y + h // 2, anchor="w", text=value,
                      fill=COLORS["fg_data"], font=("monospace", 9))

        return y + h + 2

    def _draw_text(self, y: int, text: str,
                   color: str = "") -> int:
        color = color or COLORS["text_dim"]
        self.canvas.create_text(15, y + 8, anchor="w", text=text,
                                fill=color, font=("monospace", 9))
        return y + 16

    def _draw_toggle_row(self, y: int, key: str, label: str,
                         segments: list[tuple[float, str]],
                         value: str, label_width: int = 90) -> int:
        """クリックでトグルできるバー行 (RAID/Bond 用)。"""
        expanded = self.expanded.get(key, False)
        icon = "▼" if expanded else "▶"
        row_y = self._draw_bar(y, f"{icon}{label}", segments, value,
                               label_width=label_width)
        h = row_y - y
        self._toggle_zones.append((y, y + h, key))
        return row_y

    # ─── メインループ ──────────────────────────────────────

    def _update(self) -> None:
        self.canvas.delete("all")
        self._header_zones.clear()
        self._toggle_zones.clear()
        c_width = self.canvas.winfo_width() or 850
        y = 0

        # データ収集
        cpu_data = self.cpu_col.collect()
        mem_data, swap_data = self.mem_col.collect()
        disk_data = self.disk_col.collect()
        net_data = self.net_col.collect()
        proc_data = self.proc_col.collect()
        kern_data = self.kern_col.collect()
        nvidia_data = self.nvidia_col.collect() if self.nvidia_col else []
        amd_data = self.amd_col.collect() if self.amd_col else []
        gaudi_data = self.gaudi_col.collect() if self.gaudi_col else []
        gpu_proc_data = self.gpu_proc_col.collect() if self.gpu_proc_col else []
        pcie_data = self.pcie_col.collect() if self.pcie_col else []
        nfs_data = self.nfs_col.collect() if self.nfs_col else []
        temp_data = self.temp_col.collect()

        # ─── Title Bar ────────────────────────────────────
        title_h = 32
        self.canvas.create_rectangle(0, 0, c_width, title_h,
                                     fill=COLORS["header"], outline="")
        self.canvas.create_line(0, 0, c_width, 0,
                                fill=COLORS["header_line"], width=2)
        self.canvas.create_text(c_width // 2, title_h // 2,
                                text="SYSTEM MONITOR",
                                fill=COLORS["fg"],
                                font=("monospace", 13, "bold"))
        self.canvas.create_line(0, title_h - 1, c_width, title_h - 1,
                                fill=COLORS["header_line"], width=2)
        y = title_h + 4

        # ─── Kernel ────────────────────────────────────────
        k = kern_data
        summary = f"Load:{k.load_1:.2f}  Up:{k.uptime_str}"
        y = self._draw_section_header(y, "kernel", f"Kernel {k.kernel_version}", summary)
        if self.expanded["kernel"]:
            load_frac = min(k.load_per_cpu, 1.0)
            color = COLORS["system"] if load_frac > 0.8 else COLORS["user"]
            y = self._draw_bar(y, "LOAD",
                               [(load_frac, color)],
                               f"{k.load_1:.2f}/{k.load_5:.2f}/{k.load_15:.2f}")
            y = self._draw_text(y,
                f"Up:{k.uptime_str}  Procs:{k.running_procs}/{k.total_procs}"
                f"  CtxSw:{_fmt_rate(k.ctx_switches_sec)}/s"
                f"  IRQ:{_fmt_rate(k.interrupts_sec)}/s")

        # ─── CPU ───────────────────────────────────────────
        cpu_total = next((c for c in cpu_data if c.label == "cpu"), None)
        summary = f"{cpu_total.total_pct:.1f}%" if cpu_total else ""
        y = self._draw_section_header(y, "cpu", "CPU", summary)
        if self.expanded["cpu"]:
            for c in cpu_data:
                label = "TOTAL" if c.label == "cpu" else c.label.upper()
                y = self._draw_bar(y, label,
                                   [(c.user_pct / 100, COLORS["user"]),
                                    (c.nice_pct / 100, COLORS["nice"]),
                                    (c.system_pct / 100, COLORS["system"]),
                                    (c.iowait_pct / 100, COLORS["iowait"]),
                                    (c.irq_pct / 100, COLORS["irq"])],
                                   f"{c.total_pct:.1f}%")
        else:
            if cpu_total:
                y = self._draw_bar(y, "TOTAL",
                                   [(cpu_total.user_pct / 100, COLORS["user"]),
                                    (cpu_total.system_pct / 100, COLORS["system"]),
                                    (cpu_total.iowait_pct / 100, COLORS["iowait"])],
                                   f"{cpu_total.total_pct:.1f}%")

        # ─── Memory ────────────────────────────────────────
        m = mem_data
        used_g = m.used_kb / (1024 * 1024)
        total_g = m.total_kb / (1024 * 1024)
        summary = f"{used_g:.1f}/{total_g:.1f}G ({m.used_pct:.0f}%)"
        y = self._draw_section_header(y, "memory", "Memory", summary)
        if self.expanded["memory"]:
            y = self._draw_bar(y, "MEM",
                               [(m.used_pct / 100, COLORS["user"]),
                                (m.buffers_pct / 100, COLORS["irq"]),
                                (m.cached_pct / 100, COLORS["cache"])],
                               f"{used_g:.1f}/{total_g:.1f}G")
            if swap_data.total_kb > 0:
                s = swap_data
                y = self._draw_bar(y, "SWAP",
                                   [(s.used_pct / 100, COLORS["swap"])],
                                   f"{s.used_kb / 1024 / 1024:.1f}/{s.total_kb / 1024 / 1024:.1f}G")

        # ─── Temperature ──────────────────────────────────
        if temp_data or nvidia_data or amd_data or gaudi_data:
            all_temps: list[float] = [d.primary_temp_c for d in temp_data]
            all_temps += [g.temperature_c for g in nvidia_data]
            all_temps += [g.temperature_c for g in amd_data if g.temperature_c > 0]
            all_temps += [d.temperature_c for d in gaudi_data if d.temperature_c > 0]
            max_temp = max(all_temps, default=0)
            n_sensors = len(all_temps)
            summary = f"Max:{max_temp:.0f}C  {n_sensors} sensors"
            y = self._draw_section_header(y, "temp", "Temperature", summary)
            if self.expanded.get("temp", True):
                for dev in temp_data:
                    temp = dev.primary_temp_c
                    crit = dev.primary_crit_c or 100.0
                    frac = min(temp / crit, 1.0) if crit > 0 else min(temp / 100.0, 1.0)
                    color = COLORS["gpu_temp"] if temp > crit * 0.8 else COLORS["user"]
                    val = f"{temp:.0f}C"
                    if dev.primary_crit_c > 0:
                        val += f"/{crit:.0f}C"
                    y = self._draw_bar(y, dev.display_name[:12],
                                       [(frac, color)], val)
                for g in nvidia_data:
                    frac = min(g.temperature_c / 100.0, 1.0)
                    color = COLORS["gpu_temp"] if g.temperature_c > 80 else COLORS["user"]
                    y = self._draw_bar(y, f"GPU{g.index}",
                                       [(frac, color)], f"{g.temperature_c:.0f}C")
                for g in amd_data:
                    if g.temperature_c > 0:
                        frac = min(g.temperature_c / 100.0, 1.0)
                        color = COLORS["gpu_temp"] if g.temperature_c > 80 else COLORS["user"]
                        y = self._draw_bar(y, f"AMD{g.index}",
                                           [(frac, color)], f"{g.temperature_c:.0f}C")
                for d in gaudi_data:
                    if d.temperature_c > 0:
                        frac = min(d.temperature_c / 100.0, 1.0)
                        color = COLORS["gpu_temp"] if d.temperature_c > 80 else COLORS["user"]
                        y = self._draw_bar(y, f"HL{d.index}",
                                           [(frac, color)], f"{d.temperature_c:.0f}C")

        # ─── Disk I/O ─────────────────────────────────────
        if disk_data:
            total_r = sum(d.read_bytes_sec for d in disk_data)
            total_w = sum(d.write_bytes_sec for d in disk_data)
            summary = f"R:{_fmt_bytes_sec(total_r)} W:{_fmt_bytes_sec(total_w)}"
            y = self._draw_section_header(y, "disk", f"Disk I/O ({len(disk_data)} devs)", summary)
            if self.expanded["disk"]:
                show_raid = self.expanded.get("raid_members", False)
                for d in disk_data:
                    max_bw = 1_073_741_824.0
                    segs = [(min(d.read_bytes_sec / max_bw, 0.5), COLORS["cache"]),
                            (min(d.write_bytes_sec / max_bw, 0.5), COLORS["iowait"])]
                    val = f"R:{_fmt_bytes_sec(d.read_bytes_sec)} W:{_fmt_bytes_sec(d.write_bytes_sec)}"

                    if d.raid_level:
                        # RAID デバイス: クリックでメンバー展開
                        y = self._draw_toggle_row(
                            y, "raid_members",
                            d.display_name.upper(), segs, val)
                    elif d.raid_member_of:
                        # RAID メンバー: 展開時のみ表示
                        if show_raid:
                            y = self._draw_bar(y, f" └{d.name}", segs, val)
                    else:
                        y = self._draw_bar(y, d.display_name.upper(), segs, val)
            else:
                max_bw = 1_073_741_824.0 * len(disk_data)
                y = self._draw_bar(y, "ALL",
                                   [(min(total_r / max_bw, 0.5), COLORS["cache"]),
                                    (min(total_w / max_bw, 0.5), COLORS["iowait"])],
                                   f"R:{_fmt_bytes_sec(total_r)} W:{_fmt_bytes_sec(total_w)}")

        # ─── Network ──────────────────────────────────────
        if net_data:
            total_rx = sum(n.rx_bytes_sec for n in net_data)
            total_tx = sum(n.tx_bytes_sec for n in net_data)
            summary = f"D:{_fmt_bytes_sec(total_rx)} U:{_fmt_bytes_sec(total_tx)}"
            y = self._draw_section_header(y, "network", "Network", summary)
            if self.expanded["network"]:
                show_bond = self.expanded.get("bond_members", False)
                for n in net_data:
                    max_bw = 125_000_000.0
                    tag = n.net_type.value if hasattr(n, "net_type") else "???"
                    segs = [(min(n.rx_bytes_sec / max_bw, 0.5), COLORS["net_rx"]),
                            (min(n.tx_bytes_sec / max_bw, 0.5), COLORS["net_tx"])]
                    val = f"D:{_fmt_bytes_sec(n.rx_bytes_sec)} U:{_fmt_bytes_sec(n.tx_bytes_sec)}"

                    if n.bond_mode:
                        # Bond デバイス: クリックでメンバー展開
                        y = self._draw_toggle_row(
                            y, "bond_members",
                            n.display_name, segs, val)
                    elif n.bond_member_of:
                        # Bond メンバー: 展開時のみ表示
                        if show_bond:
                            y = self._draw_bar(y, f" └{n.name}", segs, val)
                    else:
                        y = self._draw_bar(y, f"{tag} {n.name}", segs, val)

        # ─── NFS ──────────────────────────────────────────
        if nfs_data:
            summary = f"{len(nfs_data)} mounts"
            y = self._draw_section_header(y, "nfs", "NFS/SAN/NAS", summary)
            if self.expanded["nfs"]:
                for mt in nfs_data:
                    max_bw = 125_000_000.0
                    y = self._draw_bar(y, f"{mt.type_label} {mt.mount_point}"[:12],
                                       [(min(mt.read_bytes_sec / max_bw, 0.5), COLORS["net_rx"]),
                                        (min(mt.write_bytes_sec / max_bw, 0.5), COLORS["net_tx"])],
                                       f"R:{_fmt_bytes_sec(mt.read_bytes_sec)} W:{_fmt_bytes_sec(mt.write_bytes_sec)}")

        # ─── PCIe ─────────────────────────────────────────
        if pcie_data:
            summary = f"{len(pcie_data)} devices"
            y = self._draw_section_header(y, "pcie", "PCIe Devices", summary)
            if self.expanded["pcie"]:
                for d in pcie_data:
                    icon = d.icon
                    link = f"{d.gen_name} x{d.current_width}"
                    if d.io_label:
                        max_bw = max(d.current_bandwidth_gbs * 1_073_741_824, 1)
                        bar_label = f"{icon}{d.short_name}" if icon else d.short_name
                        y = self._draw_bar(y, bar_label,
                                           [(min(d.io_read_bytes_sec / max_bw, 0.5), COLORS["cache"]),
                                            (min(d.io_write_bytes_sec / max_bw, 0.5), COLORS["iowait"])],
                                           f"{link} R:{_fmt_bytes_sec(d.io_read_bytes_sec)} W:{_fmt_bytes_sec(d.io_write_bytes_sec)}",
                                           label_width=140)
                    else:
                        label = f"{icon} {d.short_name}" if icon else d.short_name
                        y = self._draw_text(y,
                            f"{label:<30s} {link} {d.current_bandwidth_gbs:5.1f} GB/s",
                            COLORS["pcie"])

        # ─── NVIDIA GPU ───────────────────────────────────
        if nvidia_data:
            summary = "  ".join(f"GPU{g.index}:{g.gpu_util_pct:.0f}%" for g in nvidia_data)
            y = self._draw_section_header(y, "nvidia", "NVIDIA GPU", summary)
            if self.expanded["nvidia"]:
                for g in nvidia_data:
                    y = self._draw_text(y, f"GPU{g.index} {g.short_name}", COLORS["fg_data"])
                    y = self._draw_bar(y, "  UTIL",
                                       [(g.gpu_util_pct / 100, COLORS["gpu_util"])],
                                       f"{g.gpu_util_pct:.0f}%")
                    y = self._draw_bar(y, "  VRAM",
                                       [(g.mem_used_pct / 100, COLORS["gpu_mem"])],
                                       f"{_fmt_mib(g.mem_used_mib)}/{_fmt_mib(g.mem_total_mib)}")
                    y = self._draw_bar(y, "  TEMP",
                                       [(g.temperature_c / 100, COLORS["gpu_temp"])],
                                       f"{g.temperature_c:.0f}C")
                    y = self._draw_bar(y, "  POWER",
                                       [(g.power_pct / 100, COLORS["gpu_power"])],
                                       f"{g.power_draw_w:.0f}/{g.power_limit_w:.0f}W")
                    if g.fan_speed_pct >= 0:
                        y = self._draw_bar(y, "  FAN",
                                           [(g.fan_speed_pct / 100, COLORS["gpu_fan"])],
                                           f"{g.fan_speed_pct:.0f}%")

        # ─── AMD GPU ──────────────────────────────────────
        if amd_data:
            summary = "  ".join(f"GPU{g.index}:{g.gpu_util_pct:.0f}%" for g in amd_data)
            y = self._draw_section_header(y, "amd", "AMD GPU (ROCm)", summary)
            if self.expanded["amd"]:
                for g in amd_data:
                    y = self._draw_text(y, f"GPU{g.index} {g.short_name}", COLORS["fg_data"])
                    y = self._draw_bar(y, "  UTIL",
                                       [(g.gpu_util_pct / 100, COLORS["gpu_util"])],
                                       f"{g.gpu_util_pct:.0f}%")
                    if g.mem_total_mib > 0:
                        y = self._draw_bar(y, "  VRAM",
                                           [(g.mem_used_pct / 100, COLORS["gpu_mem"])],
                                           f"{_fmt_mib(g.mem_used_mib)}/{_fmt_mib(g.mem_total_mib)}")

        # ─── Intel Gaudi ──────────────────────────────────
        if gaudi_data:
            summary = "  ".join(f"HL{d.index}:{d.aip_util_pct:.0f}%" for d in gaudi_data)
            y = self._draw_section_header(y, "gaudi", "Intel Gaudi", summary)
            if self.expanded["gaudi"]:
                for d in gaudi_data:
                    y = self._draw_text(y, f"HL{d.index} {d.short_name}", COLORS["fg_data"])
                    y = self._draw_bar(y, "  AIP",
                                       [(d.aip_util_pct / 100, COLORS["gpu_util"])],
                                       f"{d.aip_util_pct:.0f}%")
                    if d.mem_total_mib > 0:
                        y = self._draw_bar(y, "  HBM",
                                           [(d.mem_used_pct / 100, COLORS["gpu_mem"])],
                                           f"{_fmt_mib(d.mem_used_mib)}/{_fmt_mib(d.mem_total_mib)}")

        # ─── GPU Processes ────────────────────────────────
        if gpu_proc_data:
            summary = f"{len(gpu_proc_data)} procs"
            y = self._draw_section_header(y, "gpu_proc", "GPU Processes", summary)
            if self.expanded["gpu_proc"]:
                for p in gpu_proc_data:
                    y = self._draw_text(y,
                        f"GPU{p.gpu_index}  PID:{p.pid:>7d}  {p.name:<18s}  VRAM:{p.gpu_mem_mib:7.0f} MiB",
                        COLORS["gpu_mem"])

        # ─── Top Processes ────────────────────────────────
        if proc_data:
            top_name = proc_data[0].name if proc_data else ""
            top_cpu = proc_data[0].cpu_pct if proc_data else 0.0
            summary = f"Top: {top_name} {top_cpu:.1f}%"
            y = self._draw_section_header(y, "proc", "Top Processes", summary)
            if self.expanded["proc"]:
                for p in proc_data:
                    color = COLORS["warn"] if p.cpu_pct > 50 else COLORS["text_dim"]
                    y = self._draw_text(y,
                        f"PID:{p.pid:>7d}  {p.name:<20s}  CPU:{p.cpu_pct:5.1f}%  MEM:{p.mem_rss_mib:7.1f}M",
                        color)

        # ─── Footer ───────────────────────────────────────
        y += 6
        footer_h = 28
        self.canvas.create_rectangle(0, y, c_width, y + footer_h,
                                     fill=COLORS["header"], outline="")
        self.canvas.create_line(0, y, c_width, y,
                                fill=COLORS["header_line"], width=1)
        self.canvas.create_text(
            c_width // 2, y + footer_h // 2,
            text="Click header: expand/collapse | Click RAID/Bond: show members | +/-: interval | q/Esc: quit",
            fill=COLORS["fg_sub"], font=("monospace", 9))
        y += footer_h + 5

        # スクロール領域更新
        self.canvas.configure(scrollregion=(0, 0, c_width, y + 10))

        # 次の更新
        self.root.after(self.interval_ms, self._update)

    def run(self) -> None:
        self.root.after(500, self._update)
        self.root.mainloop()


def run_gui(args: argparse.Namespace) -> None:
    """X11 GUI を起動するエントリポイント。"""
    app = HousekeeperGui(args)
    app.run()
