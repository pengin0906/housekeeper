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
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from typing import Any


# ─── OCCT 風カラーパレット ────────────────────────────────
COLORS = {
    # 基本
    "bg": "#1a1a1a",
    "fg": "#ff002b",            # OCCT レッド
    "fg_data": "#e0e0e0",
    "fg_sub": "#cc2244",
    # ヘッダー
    "header": "#2a0a0a",
    "header_line": "#ff002b",
    # バー
    "bar_bg": "#252525",
    "bar_border": "#3a3a3a",
    # テキスト
    "text_dim": "#888888",
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
    "warn": "#ffcc00",
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


# セクションアイコン
ICONS = {
    "kernel":   "🐧",
    "cpu":      "🖥",
    "memory":   "🗄",
    "swap":     "💱",
    "temp":     "🌡",
    "disk":     "💾",
    "network":  "🌐",
    "nfs":      "📁",
    "pcie":     "🔌",
    "nvidia":   "🎮",
    "amd":      "🎮",
    "gaudi":    "🧮",
    "gpu_proc": "📊",
    "proc":     "📋",
}


def _create_icon_image(size: int) -> tk.PhotoImage:
    """指定サイズのモニターアイコンを生成。"""
    img = tk.PhotoImage(width=size, height=size)
    s = size  # 短縮名

    bg = "#1a1a1a"
    accent = "#ff002b"
    dark_accent = "#cc0022"
    bar_green = "#00cc66"
    bar_yellow = "#cccc00"
    bar_red = "#cc3333"
    bar_cyan = "#00cccc"
    frame_color = "#3a3a3a"
    screen_bg = "#252525"

    # スケール係数 (32px 基準)
    def sc(v: int) -> int:
        return v * s // 32

    # 背景
    img.put(bg, to=(0, 0, s, s))

    # モニター外枠 (オレンジ)
    img.put(accent, to=(sc(4), sc(2), sc(28), sc(4)))      # 上辺
    img.put(accent, to=(sc(4), sc(22), sc(28), sc(24)))     # 下辺
    img.put(accent, to=(sc(4), sc(2), sc(6), sc(24)))       # 左辺
    img.put(accent, to=(sc(26), sc(2), sc(28), sc(24)))     # 右辺

    # モニター内側
    img.put(screen_bg, to=(sc(6), sc(4), sc(26), sc(22)))

    # バーグラフ (4本)
    bars = [
        (sc(8), sc(8), bar_green),
        (sc(13), sc(12), bar_yellow),
        (sc(18), sc(15), bar_red),
        (sc(23), sc(10), bar_cyan),
    ]
    bar_w = max(sc(3), 2)
    for bx, top, color in bars:
        img.put(color, to=(bx, top, bx + bar_w, sc(21)))

    # モニター台座
    img.put(dark_accent, to=(sc(12), sc(25), sc(20), sc(27)))
    img.put(frame_color, to=(sc(10), sc(27), sc(22), sc(29)))

    return img


def _create_app_icon(root: tk.Tk) -> None:
    """アプリアイコンを生成して設定 (GC防止で参照を保持)。"""
    try:
        icons = [_create_icon_image(sz) for sz in (64, 32, 16)]
        root.iconphoto(True, *icons)
        # GC で消えないように root に参照を保持
        root._hk_icons = icons  # type: ignore[attr-defined]
    except tk.TclError:
        pass


class HousekeeperGui:
    """EVA風 GUI システムモニター。"""

    SECTIONS = {
        "kernel": True,
        "cpu": True,
        "memory": True,
        "swap": True,
        "temp": True,
        "disk": True,
        "network": True,
        "nfs": True,
        "pcie": True,
        "nvidia": True,
        "amd": True,
        "gaudi": True,
        "gpu_proc": False,
        "proc": False,
        # RAID / Bond / CPU コア展開
        "raid_members": False,
        "bond_members": False,
        "cpu_cores": True,
    }

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.interval_ms = int(args.interval * 1000)
        self.expanded: dict[str, bool] = dict(self.SECTIONS)

        # クリック領域: セクションヘッダー + トグル行
        self._header_zones: list[tuple[int, int, str]] = []
        self._toggle_zones: list[tuple[int, int, str]] = []
        self._help_btn_zone: tuple[int, int, int, int] = (0, 0, 0, 0)  # x1,y1,x2,y2
        self._show_help: bool = False
        self._temp_unit: str = "C"  # "C" or "F"

        # 折れ線グラフ: 履歴バッファ + モード
        self._history_len = 60  # 60サンプル (≈1分 @ 1s)
        self._history: dict[str, deque] = {}
        self._line_mode: set[str] = set()  # 折れ線モードの個別バーキー
        self._line_default: bool = True   # 新規バーをデフォルトで折れ線にする
        self._known_bars: set[str] = set()  # 既知のバーキー (初回登録用)
        self._hidden_bars: set[str] = set()  # 非表示の個別バーキー
        self._bar_zones: list[tuple[int, int, str]] = []  # (y1, y2, line_key)
        self._bar_icon_zones: list[tuple[int, int, int, int, str]] = []  # (x1,y1,x2,y2, line_key)
        self._chart_zones: list[tuple[int, int, int, int, str]] = []  # (x1,y1,x2,y2, section)
        self._bar_desc: dict[str, str] = {}  # line_key → 説明テキスト
        self._tooltip_text: str = ""  # 右クリックツールチップ
        self._tooltip_pos: tuple[int, int] = (0, 0)
        self._summary_mode: bool = True  # サマリーモード (各セクション1行)
        self._summary_expanded: set[str] = set()  # サマリーモードで展開中のセクション
        self._summary_click_zones: list[tuple[int, int, str]] = []  # (y1, y2, section)
        self._current_section: str = ""  # 描画中のセクションキー
        self._line_key_section: dict[str, str] = {}  # line_key → section (永続)

        # プロファイリング: 各コレクター・描画の所要時間 (ms)
        self._prof: dict[str, float] = {}
        self._prof_total: float = 0.0

        # 自動スケール用ピーク値 (減衰付き)
        self._peak_net_bps: float = 1_000.0    # 最低 1KB/s
        self._peak_disk_bps: float = 1_000.0
        self._peak_nfs_bps: float = 1_000.0
        self._peak_pcie_bps: float = 1_000.0

        # ウィンドウ設定
        self.root = tk.Tk()
        self.root.title("housekeeper - System Monitor")
        self.root.configure(bg=COLORS["bg"])
        _create_app_icon(self.root)
        self.root.geometry("850x900")
        self.root.minsize(600, 400)
        # 現在のワークスペースに表示
        self.root.update_idletasks()
        self._move_to_current_desktop()

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
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind_all("<Button-4>", self._on_scroll)
        self.canvas.bind_all("<Button-5>", self._on_scroll)
        self.canvas.bind_all("<MouseWheel>", self._on_scroll)
        self.root.bind("<q>", lambda e: self.root.quit())
        self.root.bind("<Escape>", lambda e: self.root.quit())
        self.root.bind("<plus>", lambda e: self._change_interval(-500))
        self.root.bind("<minus>", lambda e: self._change_interval(500))
        self.root.bind("<h>", lambda e: self._toggle_help())
        self.root.bind("<H>", lambda e: self._toggle_help())
        self.root.bind("<f>", lambda e: self._toggle_temp_unit())
        self.root.bind("<F>", lambda e: self._toggle_temp_unit())
        self.root.bind("<s>", lambda e: self._toggle_summary())
        self.root.bind("<S>", lambda e: self._toggle_summary())

        self._init_collectors()

    def _move_to_current_desktop(self) -> None:
        """xdotool で現在のワークスペースにウィンドウを移動。"""
        import subprocess
        try:
            wid = str(self.root.winfo_id())
            # 現在のデスクトップ番号を取得
            cur = subprocess.check_output(
                ["xdotool", "get_desktop"], timeout=2).strip()
            # ウィンドウをそのデスクトップに移動
            subprocess.call(
                ["xdotool", "set_desktop_for_window", wid, cur],
                timeout=2)
            subprocess.call(
                ["xdotool", "windowactivate", wid], timeout=2)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

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
        self.proc_col = ProcessCollector(top_n=0)
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
        import sys as _sys
        if _sys.platform.startswith("linux") and Path("/sys/bus/pci/devices").exists():
            self.pcie_col = _lazy_import("housekeeper.collectors.pcie", "PcieCollector")()

        # NFS/ネットワークマウント検出
        self._detect_nfs_mounts()

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

    def _detect_nfs_mounts(self) -> None:
        """クロスプラットフォームでネットワークマウントを検出。"""
        import sys as _sys
        net_fs = {"nfs", "nfs4", "nfs3", "cifs", "smbfs", "glusterfs", "ceph", "lustre"}
        if _sys.platform.startswith("linux"):
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 3 and parts[2] in net_fs:
                            self.nfs_col = _lazy_import("housekeeper.collectors.nfs", "NfsMountCollector")()
                            return
            except OSError:
                pass
        elif _sys.platform == "darwin":
            import subprocess
            try:
                out = subprocess.run(["mount"], capture_output=True, text=True, timeout=3)
                if out.returncode == 0:
                    for line in out.stdout.splitlines():
                        lower = line.lower()
                        if "nfs" in lower or "smbfs" in lower or "cifs" in lower:
                            self.nfs_col = _lazy_import("housekeeper.collectors.nfs", "NfsMountCollector")()
                            return
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif _sys.platform == "win32":
            import subprocess
            try:
                out = subprocess.run(["net", "use"], capture_output=True, text=True, timeout=5)
                if out.returncode == 0 and ("OK" in out.stdout or "Disconnected" in out.stdout):
                    self.nfs_col = _lazy_import("housekeeper.collectors.nfs", "NfsMountCollector")()
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass

    # ─── イベント ───────────────────────────────────────────

    def _on_click(self, event: Any) -> None:
        """Canvas クリック: ヘッダー行 or トグル行で展開/折りたたみ。"""
        cx = event.x
        cy = self.canvas.canvasy(event.y)

        # ツールチップ表示中なら閉じる
        if self._tooltip_text:
            self._tooltip_text = ""
            return
        # ヘルプ表示中ならクリックで閉じる
        if self._show_help:
            self._show_help = False
            return

        # ? ボタン
        bx1, by1, bx2, by2 = self._help_btn_zone
        if bx1 <= cx <= bx2 and by1 <= cy <= by2:
            self._toggle_help()
            return

        # 左端アイコンクリック (ヘッダー内なので最優先)
        for x1, y1, x2, y2, section in self._chart_zones:
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                keys = [k for k, s in self._line_key_section.items()
                        if s == section]
                # 何か変更中(折れ線 or 非表示)なら全リセット、そうでなければ全部折れ線
                if any(k in self._line_mode or k in self._hidden_bars
                       for k in keys):
                    for k in keys:
                        self._line_mode.discard(k)
                        self._hidden_bars.discard(k)
                else:
                    for k in keys:
                        self._line_mode.add(k)
                return

        # Per-bar アイコンクリック: bar↔line トグル
        for x1, y1, x2, y2, line_key in self._bar_icon_zones:
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                if line_key in self._line_mode:
                    self._line_mode.discard(line_key)
                else:
                    self._line_mode.add(line_key)
                return

        # トグル行を先にチェック (ヘッダー内にある場合があるため)
        for y1, y2, key in self._toggle_zones:
            if y1 <= cy <= y2:
                self.expanded[key] = not self.expanded[key]
                return
        for y1, y2, key in self._header_zones:
            if y1 <= cy <= y2:
                if self._summary_mode and key in self._summary_expanded:
                    self._summary_expanded.discard(key)
                else:
                    self.expanded[key] = not self.expanded[key]
                return
        # サマリーモードの行クリック: そのセクションだけフル展開
        if self._summary_mode:
            for y1, y2, section in self._summary_click_zones:
                if y1 <= cy <= y2:
                    if section in self._summary_expanded:
                        self._summary_expanded.discard(section)
                    else:
                        self._summary_expanded.add(section)
                    return

        # 個別バー/折れ線本体クリック: 非表示 (アイコンでリセット)
        for y1, y2, line_key in self._bar_zones:
            if y1 <= cy <= y2:
                self._line_mode.discard(line_key)
                self._hidden_bars.add(line_key)
                return

    def _on_right_click(self, event: Any) -> None:
        """右クリック: バーの説明をツールチップ表示。"""
        cy = self.canvas.canvasy(event.y)
        # ツールチップ表示中なら閉じる
        if self._tooltip_text:
            self._tooltip_text = ""
            return
        # バーゾーン + ヘッダーゾーンを検索
        for y1, y2, line_key in self._bar_zones:
            if y1 <= cy <= y2 and line_key in self._bar_desc:
                self._tooltip_text = self._bar_desc[line_key]
                self._tooltip_pos = (event.x, int(cy))
                return
        # ヘッダーゾーン用の説明
        section_desc = {
            "kernel": "カーネル情報: Load Average, Uptime, Context Switches, IRQ",
            "cpu": "CPU使用率: User(緑)/Nice/System(青)/IOWait(橙)/IRQ 各コア",
            "memory": "物理メモリ使用状況: Used / Buffers / Cached / Free",
            "swap": "スワップ領域: ディスク上の仮想メモリ使用状況",
            "temp": "温度センサー: CPU, マザーボード, NVMe, GPU 等の温度とファン回転数",
            "disk": "ディスクI/O: 各デバイスの読み書きスループット (RAID展開可)",
            "network": "ネットワーク: 各NICのRX/TXスループット (Bond展開可)",
            "nfs": "NFS/CIFSマウント: ネットワークファイルシステムのスループット",
            "pcie": "PCIeデバイス: リンク速度/幅, TX/RXスループット",
            "nvidia": "NVIDIA GPU: 使用率, VRAM, 温度, 消費電力, ファン (NVML/nvidia-smi)",
            "amd": "AMD GPU: 使用率, VRAM (ROCm/rocm-smi)",
            "gaudi": "Intel Gaudi: AIP使用率, HBM使用量 (hl-smi)",
        }
        for y1, y2, key in self._header_zones:
            if y1 <= cy <= y2 and key in section_desc:
                self._tooltip_text = section_desc[key]
                self._tooltip_pos = (event.x, int(cy))
                return

    def _draw_tooltip(self) -> None:
        """右クリックツールチップを描画。"""
        if not self._tooltip_text:
            return
        c = self.canvas
        c_width = self._c_width
        tx, ty = self._tooltip_pos
        text = self._tooltip_text
        # テキスト幅を推定 (monospace 9pt ≈ 7px/char)
        lines = text.split("\n")
        max_chars = max(len(l) for l in lines)
        box_w = min(max_chars * 7 + 20, c_width - 20)
        box_h = len(lines) * 16 + 16
        # 画面内に収める
        bx = min(tx, c_width - box_w - 10)
        by = ty - box_h - 4
        if by < 0:
            by = ty + 20
        # 背景
        c.create_rectangle(bx, by, bx + box_w, by + box_h,
                           fill="#222233", outline=COLORS["fg"], width=1)
        # テキスト
        text_y = by + 10
        for line in lines:
            c.create_text(bx + 10, text_y, anchor="nw", text=line,
                          fill=COLORS["fg_data"], font=("monospace", 9))
            text_y += 16

    def _on_scroll(self, event: Any) -> None:
        """スクロール: ネイティブ Canvas スクロール (再描画不要)。"""
        if event.num == 4:
            self.canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(3, "units")
        elif event.delta:
            # MouseWheel (Windows/macOS)
            self.canvas.yview_scroll(-event.delta // 120, "units")
        # ツールチップは閉じる
        if self._tooltip_text:
            self._tooltip_text = ""

    def _toggle_help(self) -> None:
        self._show_help = not self._show_help

    def _toggle_summary(self) -> None:
        self._summary_mode = not self._summary_mode

    def _toggle_temp_unit(self) -> None:
        self._temp_unit = "F" if self._temp_unit == "C" else "C"

    def _fmt_temp(self, temp_c: float, crit_c: float = 0.0) -> str:
        """温度を現在の単位でフォーマット。"""
        if self._temp_unit == "F":
            t = temp_c * 9.0 / 5.0 + 32
            s = f"{t:.0f}F"
            if crit_c > 0:
                s += f"/{crit_c * 9.0 / 5.0 + 32:.0f}F"
            return s
        s = f"{temp_c:.0f}C"
        if crit_c > 0:
            s += f"/{crit_c:.0f}C"
        return s

    def _fmt_temp_line(self, temp_c: float) -> str:
        """折れ線グラフ用の温度フォーマット (ユニット付き)。"""
        if self._temp_unit == "F":
            return f"{temp_c * 9.0 / 5.0 + 32:.0f}°F"
        return f"{temp_c:.0f}°C"

    @staticmethod
    def _gpu_temp_color(temp: float, g) -> str:
        """GPU温度の色を閾値ベースで判定。"""
        t_max = getattr(g, "temp_max_c", 0.0) or 0.0
        t_slow = getattr(g, "temp_slowdown_c", 0.0) or 0.0
        if t_slow > 0 and temp >= t_slow:
            return COLORS["gpu_temp"]   # 赤: スロットリング以上
        if t_max > 0 and temp >= t_max:
            return COLORS["warn"]        # 黄: max operating 以上
        if t_max > 0 and temp >= t_max * 0.9:
            return COLORS["warn"]        # 黄: max の 90% 以上
        return COLORS["user"]             # 緑: 正常

    def _change_interval(self, delta_ms: int) -> None:
        self.interval_ms = max(100, min(10000, self.interval_ms + delta_ms))

    def _record(self, key: str, value: float) -> None:
        """履歴データを記録。"""
        if key not in self._history:
            self._history[key] = deque(maxlen=self._history_len)
        self._history[key].append(value)

    # チャート切り替え可能なセクション
    _CHARTABLE = frozenset({"cpu", "memory", "temp", "disk", "network", "nfs",
                            "nvidia", "amd", "gaudi", "pcie"})

    # ─── 描画ヘルパー ──────────────────────────────────────

    def _draw_chart_icon(self, x: int, y: int, active: bool,
                         size: int = 16) -> None:
        """棒グラフ/折れ線グラフのミニアイコンを描画。"""
        c = self.canvas
        s = size

        def sc(v: int) -> int:
            return v * s // 16

        bg = COLORS["user"] if active else "#222233"
        c.create_rectangle(x, y, x + s, y + s,
                           fill=bg, outline=COLORS["fg"], width=1)
        if active:
            # 折れ線アイコン: ジグザグ線 (白)
            c.create_line(x + sc(2), y + sc(12), x + sc(5), y + sc(5),
                          x + sc(8), y + sc(9), x + sc(11), y + sc(3),
                          x + sc(14), y + sc(7),
                          fill="#ffffff", width=max(1, s // 8))
        else:
            # 棒グラフアイコン: 3本の縦バー (オレンジ)
            c.create_rectangle(x + sc(3), y + sc(8), x + sc(6), y + sc(14),
                               fill=COLORS["fg"], outline="")
            c.create_rectangle(x + sc(7), y + sc(4), x + sc(10), y + sc(14),
                               fill=COLORS["fg"], outline="")
            c.create_rectangle(x + sc(11), y + sc(6), x + sc(14), y + sc(14),
                               fill=COLORS["fg"], outline="")

    def _draw_section_header(self, y: int, key: str, title: str,
                             summary: str = "") -> int:
        """OCCT風セクションヘッダー: 赤い左ボーダー + クリーンなタイトル。"""
        h = 24
        self._header_zones.append((y, y + h, key))
        # ビューポート外ならスキップ
        if y + h < self._view_top or y > self._view_bot:
            return y + h + 2

        c = self.canvas
        c_width = self._c_width
        expanded = self.expanded.get(key, True)
        fold_icon = "▼" if expanded else "▶"

        # 背景 + 赤い左ボーダー
        c.create_rectangle(0, y, c_width, y + h,
                           fill=COLORS["header"], outline="")
        c.create_rectangle(0, y, 3, y + h, fill=COLORS["fg"], outline="")

        # 左端: チャートアイコン
        x_cursor = 8
        lm = self._line_mode
        in_line = any(k in lm for k, s in self._line_key_section.items()
                      if s == key)
        self._draw_chart_icon(x_cursor, y + (h - 16) // 2, in_line)
        self._chart_zones.append((x_cursor, y, x_cursor + 16, y + h, key))
        x_cursor += 20

        # タイトル + サマリーを1行にまとめて create_text 削減
        section_icon = ICONS.get(key, "")
        header_text = f"{fold_icon} {section_icon} {title}" if section_icon else f"{fold_icon} {title}"
        c.create_text(x_cursor, y + h // 2, anchor="w", text=header_text,
                      fill=COLORS["fg_data"], font=("monospace", 11, "bold"))
        if summary:
            c.create_text(c_width - 10, y + h // 2, anchor="e", text=summary,
                          fill=COLORS["text_dim"], font=("monospace", 9))

        # 下ライン
        c.create_line(0, y + h - 1, c_width, y + h - 1,
                      fill=COLORS["bar_border"], width=1)

        return y + h + 2

    def _draw_bar(self, y: int, label: str, segments: list[tuple[float, str]],
                  value: str, label_width: int = 90,
                  line_key: str = "",
                  line_series: list[tuple[str, str]] | None = None,
                  line_max: float = 100.0,
                  line_fmt: str = "{:.1f}",
                  line_fmt_fn=None,
                  desc: str = "") -> int:
        """EVA風バーメーター。line_key指定時は左アイコンで折れ線に切替可能。"""
        # 説明テキスト登録
        if line_key and desc:
            self._bar_desc[line_key] = desc
        # line_key → section 登録 (永続)
        if line_key and self._current_section:
            self._line_key_section[line_key] = self._current_section

        # 新規バーはデフォルトで折れ線モード
        if line_key and line_key not in self._known_bars:
            self._known_bars.add(line_key)
            if self._line_default:
                self._line_mode.add(line_key)

        # 非表示モード: 描画スキップ
        if line_key and line_key in self._hidden_bars:
            return y

        # 描画高さを事前計算
        is_line = bool(line_key and line_key in self._line_mode and line_series)
        item_h = 34 if is_line else 18  # 折れ線=30+4, バー=16+2
        # ビューポート外 → 描画スキップ (y位置とゾーンだけ進める)
        if y + item_h < self._view_top or y > self._view_bot:
            end_y = y + item_h
            if line_key:
                self._bar_zones.append((y, end_y, line_key))
                self._bar_icon_zones.append((0, y, 18, end_y, line_key))
            return end_y

        # Per-bar chart icon (左端に小さいアイコン)
        x_off = 10
        if line_key:
            ico_sz = 12
            ico_x = 1
            ico_y = y + 2
            active = line_key in self._line_mode
            self._draw_chart_icon(ico_x, ico_y, active, size=ico_sz)
            x_off = 16

        # 折れ線グラフモード
        if is_line:
            r = self._draw_line_chart(y, label, line_series,
                                      max_val=line_max, height=30,
                                      fmt_val=line_fmt,
                                      fmt_fn=line_fmt_fn,
                                      label_width=label_width,
                                      x_offset=x_off)
            self._bar_zones.append((y, r, line_key))
            self._bar_icon_zones.append((0, y, 18, r, line_key))
            return r

        c = self.canvas
        c_width = self._c_width
        lw = label_width
        bw = max(c_width - lw - x_off - 180, 100)
        h = 16
        x = x_off

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

        # バーゾーン記録 (個別クリック用)
        end_y = y + h + 2
        if line_key:
            self._bar_zones.append((y, end_y, line_key))
            self._bar_icon_zones.append((0, y, 18, end_y, line_key))

        return end_y

    def _draw_text(self, y: int, text: str,
                   color: str = "", hide_key: str = "") -> int:
        if hide_key:
            if self._current_section:
                self._line_key_section[hide_key] = self._current_section
            if hide_key in self._hidden_bars:
                return y
        end_y = y + 16
        if end_y >= self._view_top and y <= self._view_bot:
            color = color or COLORS["text_dim"]
            self.canvas.create_text(15, y + 8, anchor="w", text=text,
                                    fill=color, font=("monospace", 9))
        if hide_key:
            self._bar_zones.append((y, end_y, hide_key))
        return end_y

    def _draw_line_chart(self, y: int, label: str,
                         series: list[tuple[str, str]],
                         max_val: float = 100.0,
                         height: int = 60,
                         fmt_val: str = "{:.1f}",
                         fmt_fn=None,
                         label_width: int = 90,
                         x_offset: int = 10) -> int:
        """折れ線グラフを描画。series = [(history_key, color), ...]

        max_val:
          >0  : 0〜max_val の固定スケール
          0   : 0〜(データ最大*1.2) のオートスケール
          <0  : データの min-max レンジでオートレンジ (温度等向き)
        """
        c = self.canvas
        c_width = self._c_width
        lw = label_width
        gw = max(c_width - lw - x_offset - 110, 100)
        gh = height
        gx = x_offset + lw
        gy = y + 2

        # ラベル
        c.create_text(x_offset, gy + gh // 2, anchor="w", text=label,
                      fill=COLORS["fg"], font=("monospace", 10, "bold"))

        # グラフ背景
        c.create_rectangle(gx, gy, gx + gw, gy + gh,
                           fill=COLORS["bar_bg"], outline=COLORS["bar_border"])

        # 全データ収集
        all_vals: list[float] = []
        for hkey, _ in series:
            if hkey in self._history:
                all_vals.extend(self._history[hkey])

        # スケール決定
        min_val = 0.0
        if max_val < 0:
            # オートレンジ: データの min-max ± パディング
            if all_vals:
                d_min = min(all_vals)
                d_max = max(all_vals)
                margin = max((d_max - d_min) * 0.3, 2.0)
                min_val = max(d_min - margin, 0.0)
                max_val = d_max + margin
            else:
                max_val = 100.0
        elif max_val == 0:
            max_val = max(all_vals, default=1.0) * 1.2
            if max_val <= 0:
                max_val = 1.0

        val_range = max_val - min_val
        if val_range <= 0:
            val_range = 1.0

        # Y軸レンジ表示 (上=max, 下=min)
        if min_val > 0:
            max_lbl = fmt_fn(max_val) if fmt_fn else f"{max_val:.0f}"
            min_lbl = fmt_fn(min_val) if fmt_fn else f"{min_val:.0f}"
            c.create_text(gx + gw + 4, gy, anchor="nw",
                          text=max_lbl, fill=COLORS["text_dim"],
                          font=("monospace", 7))
            c.create_text(gx + gw + 4, gy + gh, anchor="sw",
                          text=min_lbl, fill=COLORS["text_dim"],
                          font=("monospace", 7))

        # グリッドライン (50%)
        mid_y = gy + gh * 0.5
        c.create_line(gx, mid_y, gx + gw, mid_y,
                      fill=COLORS["bar_border"], dash=(2, 4))

        # 各系列を描画 (時間軸を history_len 基準で固定)
        gy_gh = gy + gh
        hl = self._history_len
        x_step = gw / max(hl - 1, 1)
        for hkey, color in series:
            if hkey not in self._history or len(self._history[hkey]) < 2:
                continue
            data = self._history[hkey]
            n = len(data)
            offset = hl - n  # 右詰め: 最新データが右端に来る
            inv_range = 1.0 / val_range
            flat = []
            for i, v in enumerate(data):
                flat.append(gx + (offset + i) * x_step)
                f = (v - min_val) * inv_range
                if f < 0.0: f = 0.0
                elif f > 1.0: f = 1.0
                flat.append(gy_gh - f * gh)
            if len(flat) >= 4:
                c.create_line(*flat, fill=color, width=1, smooth=False)

        # 最新値テキスト (各系列) - グラフ右端、中央付近に表示
        vx = gx + gw + 4
        n_series = sum(1 for hk, _ in series
                       if hk in self._history and self._history[hk])
        vy = gy + (gh - n_series * 12) // 2
        if min_val > 0:
            vy = max(vy, gy + 10)  # レンジ上限ラベルと重ならない
        for hkey, color in series:
            if hkey in self._history and self._history[hkey]:
                latest = self._history[hkey][-1]
                val_text = fmt_fn(latest) if fmt_fn else fmt_val.format(latest)
                c.create_text(vx, vy, anchor="nw", text=val_text,
                              fill=color, font=("monospace", 9, "bold"))
                vy += 12

        return y + gh + 4

    def _draw_summary_row(self, y: int, label: str,
                          series: list[tuple[str, str]],
                          value: str,
                          max_val: float = 0,
                          fmt_fn=None,
                          legend: list[str] | None = None,
                          section: str = "",
                          values: list[str] | None = None) -> int:
        """サマリーモード用: ラベル + 折れ線 + 値。通常バーと同程度のコンパクトサイズ。"""
        h = self._summary_row_h
        if y + h < self._view_top or y > self._view_bot:
            return y + h
        c = self.canvas
        c_width = self._c_width
        # 通常モードと同じフォントサイズ・グラフ位置
        font_sz = 10
        font_sm = 9
        pad = 3
        # 通常モード _draw_line_chart と同じ: gx = x_offset(16) + lw(90) = 106
        # グラフ右端 = c_width - 110
        lw = 80
        gx = 16 + lw  # 96
        gw = max(c_width - gx - 110, 100)
        gy = y + pad
        gh = h - pad * 2

        # 行背景 (交互色) + 下線
        bg = COLORS["bar_bg"] if (y // h) % 2 == 0 else COLORS["bg"]
        c.create_rectangle(0, y, c_width, y + h, fill=bg, outline="")
        c.create_line(0, y + h - 1, c_width, y + h - 1,
                      fill=COLORS["bar_border"], width=1)

        # ラベル (左端)
        c.create_text(4, y + h // 2, anchor="w", text=label,
                      fill=COLORS["fg"], font=("monospace", font_sz, "bold"))

        # グラフ背景
        c.create_rectangle(gx, gy, gx + gw, gy + gh,
                           fill=COLORS["bg"], outline=COLORS["bar_border"])

        # 折れ線描画 (各系列を個別に 0-1 正規化, 時間軸は history_len 基準)
        gy_gh = gy + gh
        hl = self._history_len
        x_step_s = gw / max(hl - 1, 1)
        for hkey, color in series:
            if hkey not in self._history or len(self._history[hkey]) < 2:
                continue
            data = self._history[hkey]
            d_min = min(data)
            d_max = max(data)
            if max_val < 0:
                margin = max((d_max - d_min) * 0.3, 2.0)
                s_min = max(d_min - margin, 0.0)
                s_max = d_max + margin
            elif max_val == 0:
                s_min = 0.0
                s_max = d_max * 1.2 if d_max > 0 else 1.0
            else:
                s_min = 0.0
                s_max = max_val
            s_range = s_max - s_min
            if s_range <= 0:
                s_range = 1.0
            n = len(data)
            offset = hl - n  # 右詰め: 最新データが右端
            inv_range = 1.0 / s_range
            flat = []
            for i, v in enumerate(data):
                flat.append(gx + (offset + i) * x_step_s)
                f = (v - s_min) * inv_range
                if f < 0.0: f = 0.0
                elif f > 1.0: f = 1.0
                flat.append(gy_gh - f * gh)
            if len(flat) >= 4:
                c.create_line(*flat, fill=color, width=3, smooth=False)

        # 値 + 凡例 (グラフの左側に表示) — tkinter Font で実測
        import tkinter.font as tkfont
        f_sm = tkfont.Font(family="monospace", size=font_sm, weight="bold")
        f_lg = tkfont.Font(family="monospace", size=font_sz, weight="bold")
        line_h = f_sm.metrics("linespace") // 2 + 2

        def _measure(txt: str) -> int:
            return f_sm.measure(txt)

        # 値 — グラフ右側 (通常モードと同じ位置)
        rvx = gx + gw + 4
        avail_w = c_width - rvx - 2

        if values and len(values) == len(series):
            n_vals = len(values)
            row1 = values[: (n_vals + 1) // 2]
            col1 = [series[i][1] for i in range((n_vals + 1) // 2)]
            row2 = values[(n_vals + 1) // 2:]
            col2 = [series[i][1] for i in range((n_vals + 1) // 2, n_vals)]
            gap = _measure(" ")
            max_row_w = max(
                sum(_measure(v) for v in row1) + gap * (len(row1) - 1),
                sum(_measure(v) for v in row2) + gap * (len(row2) - 1))
            if max_row_w > avail_w:
                values = [v[0].upper() + v[v.find(":"):] if v.find(":") > 1 else v for v in values]
                row1 = values[: (n_vals + 1) // 2]
                row2 = values[(n_vals + 1) // 2:]
            # 2段表示 — グラフ右側
            y_top = y + h // 2 - line_h
            y_bot = y + h // 2 + line_h
            rx = rvx
            for txt, clr in zip(row1, col1):
                c.create_text(rx, y_top, anchor="w", text=txt,
                              fill=clr, font=f_sm)
                rx += _measure(txt) + gap
            rx = rvx
            for txt, clr in zip(row2, col2):
                c.create_text(rx, y_bot, anchor="w", text=txt,
                              fill=clr, font=f_sm)
                rx += _measure(txt) + gap
        else:
            # 値テキスト + 凡例 — グラフ右側
            if legend and len(legend) == len(series):
                c.create_text(rvx, y + h // 2 - line_h, anchor="w", text=value,
                              fill=COLORS["fg_data"], font=f_lg)
                lx = rvx
                ly = y + h // 2 + line_h
                leg_gap = _measure(" ")
                for (_hkey, color), leg_text in zip(series, legend):
                    c.create_line(lx, ly, lx + 10, ly, fill=color, width=2)
                    c.create_text(lx + 13, ly, anchor="w", text=leg_text,
                                  fill=color, font=f_sm)
                    lx += 13 + _measure(leg_text) + leg_gap
            else:
                c.create_text(rvx, y + h // 2, anchor="w", text=value,
                              fill=COLORS["fg_data"], font=f_lg)

        # クリックゾーン登録
        if section:
            self._summary_click_zones.append((y, y + h, section))

        return y + h

    def _draw_toggle_row(self, y: int, key: str, label: str,
                         segments: list[tuple[float, str]],
                         value: str, label_width: int = 90,
                         line_key: str = "",
                         line_series: list[tuple[str, str]] | None = None,
                         line_max: float = 100.0,
                         line_fmt: str = "{:.1f}",
                         line_fmt_fn=None,
                         desc: str = "") -> int:
        """クリックでトグルできるバー行 (RAID/Bond 用)。"""
        expanded = self.expanded.get(key, False)
        icon = "▼" if expanded else "▶"
        row_y = self._draw_bar(y, f"{icon}{label}", segments, value,
                               label_width=label_width,
                               line_key=line_key, line_series=line_series,
                               line_max=line_max, line_fmt=line_fmt,
                               line_fmt_fn=line_fmt_fn, desc=desc)
        h = row_y - y
        self._toggle_zones.append((y, y + h, key))
        return row_y

    # ─── メインループ ──────────────────────────────────────

    def _timed_collect(self, name: str, collector, *args):
        """コレクターを呼び出し、所要時間を記録。"""
        t0 = time.perf_counter()
        result = collector.collect(*args)
        self._prof[name] = (time.perf_counter() - t0) * 1000
        return result

    def _update(self) -> None:
        t_frame_start = time.perf_counter()

        # データ収集 - ファスト/スロー分離
        self._prof.clear()
        now_mono = time.monotonic()
        # ファスト (毎フレーム): cpu, mem, disk, net, kern
        cpu_data = self._timed_collect("cpu", self.cpu_col)
        mem_data, swap_data = self._timed_collect("mem", self.mem_col)
        disk_data = self._timed_collect("disk", self.disk_col)
        net_data = self._timed_collect("net", self.net_col)
        kern_data = self._timed_collect("kern", self.kern_col)
        # スロー (3秒キャッシュ): proc, nvidia, gpu_proc, nfs
        if not hasattr(self, "_slow_cache_time"):
            self._slow_cache_time = 0.0
            self._slow_proc: list = []
            self._slow_nvidia: list = []
            self._slow_amd: list = []
            self._slow_gaudi: list = []
            self._slow_gpu_proc: list = []
            self._slow_nfs: list = []
        if now_mono - self._slow_cache_time >= 3.0:
            self._slow_cache_time = now_mono
            self._slow_proc = self._timed_collect("proc", self.proc_col)
            self._slow_nvidia = self._timed_collect("nvidia", self.nvidia_col) if self.nvidia_col else []
            self._slow_amd = self._timed_collect("amd", self.amd_col) if self.amd_col else []
            self._slow_gaudi = self._timed_collect("gaudi", self.gaudi_col) if self.gaudi_col else []
            self._slow_gpu_proc = self._timed_collect("gpu_proc", self.gpu_proc_col) if self.gpu_proc_col else []
            self._slow_nfs = self._timed_collect("nfs", self.nfs_col) if self.nfs_col else []
        proc_data = self._slow_proc
        nvidia_data = self._slow_nvidia
        amd_data = self._slow_amd
        gaudi_data = self._slow_gaudi
        gpu_proc_data = self._slow_gpu_proc
        nfs_data = self._slow_nfs
        # 超スロー (5秒キャッシュ): pcie, temp
        if not hasattr(self, "_vslow_cache_time"):
            self._vslow_cache_time = 0.0
            self._vslow_pcie: list = []
            self._vslow_temp: list = []
        if now_mono - self._vslow_cache_time >= 5.0:
            self._vslow_cache_time = now_mono
            self._vslow_pcie = self._timed_collect("pcie", self.pcie_col) if self.pcie_col else []
            self._vslow_temp = self._timed_collect("temp", self.temp_col)
        pcie_data = self._vslow_pcie
        temp_data = self._vslow_temp
        t_collect_end = time.perf_counter()
        self._prof["_collect"] = (t_collect_end - t_frame_start) * 1000

        # 描画データキャッシュ (スクロール時の即時再描画用)
        self._last_draw_data = (
            cpu_data, mem_data, swap_data, disk_data, net_data,
            kern_data, proc_data, nvidia_data, amd_data, gaudi_data,
            gpu_proc_data, nfs_data, pcie_data, temp_data,
        )

        self._draw(*self._last_draw_data)

        # 次の更新
        self.root.after(self.interval_ms, self._update)

    def _draw(self, cpu_data, mem_data, swap_data, disk_data, net_data,
              kern_data, proc_data, nvidia_data, amd_data, gaudi_data,
              gpu_proc_data, nfs_data, pcie_data, temp_data) -> None:
        """キャッシュ済みデータで描画。"""
        t_draw_start = time.perf_counter()
        self.canvas.delete("all")
        self._header_zones.clear()
        self._toggle_zones.clear()
        self._bar_zones.clear()
        self._bar_icon_zones.clear()
        self._chart_zones.clear()
        self._summary_click_zones.clear()
        self._c_width = self.canvas.winfo_width() or 850
        c_width = self._c_width
        c_height_vis = self.canvas.winfo_height() or 900
        # ビューポート + 上下マージン (スクロール時の空白防止)
        vt = self.canvas.canvasy(0)
        self._view_top = vt - c_height_vis
        self._view_bot = vt + c_height_vis + c_height_vis
        y = 0

        # ─── Title Bar ────────────────────────────────────
        title_h = 32
        self.canvas.create_rectangle(0, 0, c_width, title_h,
                                     fill=COLORS["header"], outline="")
        self.canvas.create_line(0, 0, c_width, 0,
                                fill=COLORS["header_line"], width=2)
        title_text = "HOUSEKEEPER [SUMMARY]" if self._summary_mode else "HOUSEKEEPER"
        self.canvas.create_text(c_width // 2, title_h // 2,
                                text=title_text,
                                fill=COLORS["fg"],
                                font=("monospace", 14, "bold"))
        # ? ヘルプボタン (右端)
        btn_w, btn_h = 28, 22
        btn_x = c_width - btn_w - 8
        btn_y = (title_h - btn_h) // 2
        self.canvas.create_rectangle(btn_x, btn_y, btn_x + btn_w, btn_y + btn_h,
                                     fill=COLORS["bar_bg"], outline=COLORS["fg"])
        self.canvas.create_text(btn_x + btn_w // 2, btn_y + btn_h // 2,
                                text="?", fill=COLORS["fg"],
                                font=("monospace", 12, "bold"))
        self._help_btn_zone = (btn_x, btn_y, btn_x + btn_w, btn_y + btn_h)

        self.canvas.create_line(0, title_h - 1, c_width, title_h - 1,
                                fill=COLORS["header_line"], width=2)
        y = title_h

        # ─── Kernel ────────────────────────────────────────
        sm = self._summary_mode
        se = self._summary_expanded  # サマリーモードでフル展開中のセクション

        # サマリーモード: 画面に合わせつつ上限付き
        if sm:
            c_height = self.canvas.winfo_height() or 900
            n_rows = 0
            if "kernel" not in se: n_rows += 1
            if cpu_data and "cpu" not in se: n_rows += 1
            if mem_data and "memory" not in se: n_rows += 1
            if swap_data and swap_data.total_kb > 0 and "swap" not in se: n_rows += 1
            if (temp_data or nvidia_data or amd_data or gaudi_data) and "temp" not in se: n_rows += 1
            if disk_data and "disk" not in se: n_rows += 1
            if net_data and "network" not in se: n_rows += 1
            if nfs_data and "nfs" not in se: n_rows += 1
            if pcie_data and "pcie" not in se: n_rows += 1
            if nvidia_data and "nvidia" not in se: n_rows += len(nvidia_data)
            if amd_data and "amd" not in se: n_rows += len(amd_data)
            if gaudi_data and "gaudi" not in se: n_rows += len(gaudi_data)
            available_h = c_height - title_h
            self._summary_row_h = max(available_h // max(n_rows, 1), 30)
        else:
            self._summary_row_h = 40

        k = kern_data
        self._record("load", k.load_per_cpu * 100)
        if sm and "kernel" not in se:
            y = self._draw_summary_row(y, "LOAD",
                                       [("load", COLORS["user"])],
                                       f"{k.load_1:.2f}  Up:{k.uptime_str}", max_val=0,
                                       legend=["load/cpu"], section="kernel")
        else:
            summary = f"Load:{k.load_1:.2f}  Up:{k.uptime_str}"
            y = self._draw_section_header(y, "kernel", f"Kernel {k.kernel_version}", summary)
            self._current_section = "kernel"
            if self.expanded["kernel"]:
                load_frac = min(k.load_per_cpu, 1.0)
                color = COLORS["warn"] if load_frac > 0.8 else COLORS["user"]
                y = self._draw_bar(y, "LOAD",
                                   [(load_frac, color)],
                                   f"{k.load_1:.2f}/{k.load_5:.2f}/{k.load_15:.2f}",
                                   line_key="load",
                                   line_series=[("load", COLORS["user"])],
                                   line_max=0, line_fmt="{:.0f}%",
                                   desc="Load Average: 実行待ちプロセス数の平均\n1分/5分/15分 (CPU数で正規化)")
                y = self._draw_text(y,
                    f"Up:{k.uptime_str}  Procs:{k.running_procs}/{k.total_procs}"
                    f"  CtxSw:{_fmt_rate(k.ctx_switches_sec)}/s"
                    f"  IRQ:{_fmt_rate(k.interrupts_sec)}/s")

        # ─── CPU ───────────────────────────────────────────
        cpu_total = next((c for c in cpu_data if c.label == "cpu"), None)
        # CPU温度を取得
        cpu_temp_dev = next((d for d in temp_data if d.category == "CPU"), None)
        cpu_temp_str = f" {self._fmt_temp(cpu_temp_dev.primary_temp_c)}" if cpu_temp_dev else ""
        # CPUファンを収集 (hwmon CPU + IPMI Mainboard の CPU_FAN*)
        cpu_fans = []
        if cpu_temp_dev:
            cpu_fans.extend(cpu_temp_dev.fans)
        mb_dev = next((d for d in temp_data if d.category == "Mainboard"), None)
        if mb_dev:
            for fan in mb_dev.fans:
                if "CPU" in fan.label.upper():
                    cpu_fans.append(fan)
        cpu_fan_str = ""
        if cpu_fans:
            cpu_fan_str = f" {cpu_fans[0].rpm}rpm"
        # 履歴記録 (全コア)
        for cd in cpu_data:
            hk = cd.label  # "cpu", "cpu0", "cpu1", ...
            self._record(f"{hk}_user", cd.user_pct)
            self._record(f"{hk}_sys", cd.system_pct)
            self._record(f"{hk}_iowait", cd.iowait_pct)
        summary = f"{cpu_total.total_pct:.1f}%{cpu_temp_str}{cpu_fan_str}" if cpu_total else ""
        if sm and "cpu" not in se:
            if cpu_total:
                y = self._draw_summary_row(y, "🖥CPU",
                                           [("cpu_user", COLORS["user"]),
                                            ("cpu_sys", COLORS["system"]),
                                            ("cpu_iowait", COLORS["iowait"])],
                                           f"{cpu_total.total_pct:.0f}%{cpu_temp_str}", max_val=0,
                                           legend=["user", "sys", "iowait"], section="cpu")
        else:
            y = self._draw_section_header(y, "cpu", "CPU", summary)
            self._current_section = "cpu"
        if (not sm and self.expanded["cpu"]) or "cpu" in se:
            # CPU温度バー
            if cpu_temp_dev:
                temp = cpu_temp_dev.primary_temp_c
                crit = cpu_temp_dev.primary_crit_c or 100.0
                frac = min(temp / crit, 1.0) if crit > 0 else min(temp / 100.0, 1.0)
                color = COLORS["gpu_temp"] if temp > crit * 0.8 else COLORS["user"]
                val = self._fmt_temp(temp, cpu_temp_dev.primary_crit_c)
                self._record("cpu_temp", temp)
                y = self._draw_bar(y, "🖥🌡TEMP", [(frac, color)], val,
                                   line_key="cpu_temp",
                                   line_series=[("cpu_temp", color)],
                                   line_max=-1, line_fmt_fn=self._fmt_temp_line,
                                   desc=f"CPUパッケージ温度 ({cpu_temp_dev.name})\nCritical: {crit:.0f}°C")
            # CPUファン (hwmon + IPMI)
            for fi, fan in enumerate(cpu_fans):
                max_rpm = 5000.0
                frac = min(fan.rpm / max_rpm, 1.0) if max_rpm > 0 else 0.0
                cfk = f"cpu_fan{fi}"
                self._record(cfk, fan.rpm)
                y = self._draw_bar(y, f"🖥💨{fan.label}"[:12],
                                   [(frac, COLORS["gpu_fan"])],
                                   f"{fan.rpm} RPM",
                                   line_key=cfk,
                                   line_series=[(cfk, COLORS["gpu_fan"])],
                                   line_max=0, line_fmt="{:.0f}")
            # CPU 合計 (トグル行: ▶/▼CPU クリックで個別コア展開)
            if cpu_total:
                y = self._draw_toggle_row(y, "cpu_cores", "CPU",
                                          [(cpu_total.user_pct / 100, COLORS["user"]),
                                           (cpu_total.nice_pct / 100, COLORS["nice"]),
                                           (cpu_total.system_pct / 100, COLORS["system"]),
                                           (cpu_total.iowait_pct / 100, COLORS["iowait"]),
                                           (cpu_total.irq_pct / 100, COLORS["irq"])],
                                          f"{cpu_total.total_pct:.1f}%",
                                          line_key="cpu",
                                          line_series=[("cpu_user", COLORS["user"]),
                                                       ("cpu_sys", COLORS["system"]),
                                                       ("cpu_iowait", COLORS["iowait"])],
                                          line_max=0, line_fmt="{:.0f}%",
                                          desc="CPU全コア合計: 緑=User 青=System 橙=IOWait\nクリックで個別コア展開")
            # 個別コア (cpu_cores 展開時のみ)
            if self.expanded.get("cpu_cores", True):
                for cd in cpu_data:
                    if cd.label == "cpu":
                        continue  # TOTAL は上で表示済み
                    hk = cd.label
                    y = self._draw_bar(y, hk.upper(),
                                       [(cd.user_pct / 100, COLORS["user"]),
                                        (cd.nice_pct / 100, COLORS["nice"]),
                                        (cd.system_pct / 100, COLORS["system"]),
                                        (cd.iowait_pct / 100, COLORS["iowait"]),
                                        (cd.irq_pct / 100, COLORS["irq"])],
                                       f"{cd.total_pct:.1f}%",
                                       line_key=hk,
                                       line_series=[(f"{hk}_user", COLORS["user"]),
                                                    (f"{hk}_sys", COLORS["system"]),
                                                    (f"{hk}_iowait", COLORS["iowait"])],
                                       line_max=0, line_fmt="{:.0f}%",
                                       desc=f"論理コア {hk}: 緑=User 青=System 橙=IOWait")

        # ─── Memory ────────────────────────────────────────
        m = mem_data
        used_g = m.used_kb / (1024 * 1024)
        total_g = m.total_kb / (1024 * 1024)
        self._record("mem_used", m.used_pct)
        self._record("mem_cached", m.cached_pct)
        summary = f"{used_g:.1f}/{total_g:.1f}G ({m.used_pct:.0f}%)"
        if sm and "memory" not in se:
            y = self._draw_summary_row(y, "🗄MEM",
                                       [("mem_used", COLORS["user"]),
                                        ("mem_cached", COLORS["cache"])],
                                       f"{used_g:.1f}/{total_g:.1f}G {m.used_pct:.0f}%", max_val=0,
                                       legend=["used", "cache"], section="memory")
        else:
            y = self._draw_section_header(y, "memory", "Memory", summary)
            self._current_section = "memory"
        # OOM 警告判定: available = free + cached + buffers
        avail_kb = m.free_kb + m.cached_kb + m.buffers_kb
        avail_pct = 100.0 * avail_kb / m.total_kb if m.total_kb else 100.0
        swap_danger = swap_data.total_kb > 0 and swap_data.used_pct > 80
        oom_level = 0  # 0=正常, 1=注意, 2=危険, 3=OOM直前
        if avail_pct < 3 or (avail_pct < 5 and swap_danger):
            oom_level = 3
        elif avail_pct < 5 or (avail_pct < 10 and swap_danger):
            oom_level = 2
        elif avail_pct < 10:
            oom_level = 1

        if oom_level >= 2:
            avail_g = avail_kb / (1024 * 1024)
            oom_msgs = {
                3: f"⚠ OOM 危険: 空きメモリ残 {avail_g:.2f}G ({avail_pct:.1f}%) - OOM Killer 発動直前",
                2: f"⚠ メモリ逼迫: 空き {avail_g:.1f}G ({avail_pct:.1f}%) - プロセスが強制終了される可能性",
            }
            oom_color = "#ff0000" if oom_level == 3 else COLORS["warn"]
            y_warn = y if not self.expanded["memory"] else y
            # ヘッダーの直後に警告表示
            c = self.canvas
            c_w = self._c_width
            c.create_rectangle(10, y, c_w - 10, y + 18,
                               fill="#440000" if oom_level == 3 else "#332200",
                               outline=oom_color)
            c.create_text(c_w // 2, y + 9, text=oom_msgs[oom_level],
                          fill=oom_color, font=("monospace", 9, "bold"))
            y += 20
        elif oom_level == 1:
            y = self._draw_text(y,
                f"⚠ メモリ注意: 空き {avail_kb / (1024*1024):.1f}G ({avail_pct:.1f}%)",
                COLORS["warn"])

        if (not sm and self.expanded["memory"]) or "memory" in se:
            cached_g = m.cached_kb / (1024 * 1024)
            buffers_g = m.buffers_kb / (1024 * 1024)
            free_g = m.free_kb / (1024 * 1024)
            y = self._draw_bar(y, "🗄USED",
                               [(m.used_pct / 100, COLORS["user"])],
                               f"{used_g:.1f}/{total_g:.1f}G",
                               line_key="mem",
                               line_series=[("mem_used", COLORS["user"])],
                               line_max=0, line_fmt="{:.0f}%",
                               desc=f"使用中メモリ: プロセスが確保している領域\n総容量: {total_g:.1f}GB")
            self._record("mem_buffers", m.buffers_pct)
            y = self._draw_bar(y, "🗄CACHE",
                               [(m.cached_pct / 100, COLORS["cache"])],
                               f"{cached_g:.1f}G ({m.cached_pct:.0f}%)",
                               line_key="mem_cache",
                               line_series=[("mem_cached", COLORS["cache"])],
                               line_max=0, line_fmt="{:.0f}%",
                               desc="ページキャッシュ: ファイルI/O高速化用\nメモリ不足時は自動解放される")
            if m.buffers_kb > 0:
                y = self._draw_bar(y, "🗄BUF",
                                   [(m.buffers_pct / 100, COLORS["irq"])],
                                   f"{buffers_g:.2f}G ({m.buffers_pct:.0f}%)",
                                   line_key="mem_buf",
                                   line_series=[("mem_buffers", COLORS["irq"])],
                                   line_max=0, line_fmt="{:.0f}%",
                                   desc="バッファ: ブロックデバイスI/O用のカーネルバッファ")

        # ─── Swap ─────────────────────────────────────────
        if swap_data.total_kb > 0:
            s = swap_data
            self._record("swap_used", s.used_pct)
            swap_g = s.used_kb / 1024 / 1024
            swap_total_g = s.total_kb / 1024 / 1024
            swap_summary = f"{swap_g:.1f}/{swap_total_g:.1f}G ({s.used_pct:.0f}%)"
            if sm and "swap" not in se:
                y = self._draw_summary_row(y, "💱SWAP",
                                           [("swap_used", COLORS["swap"])],
                                           swap_summary, max_val=0,
                                           legend=["used"], section="swap")
            else:
                y = self._draw_section_header(y, "swap", "Swap", swap_summary)
                self._current_section = "swap"
            if (not sm and self.expanded.get("swap", True)) or "swap" in se:
                y = self._draw_bar(y, "💱SWAP",
                                   [(s.used_pct / 100, COLORS["swap"])],
                                   f"{swap_g:.1f}/{swap_total_g:.1f}G",
                                   line_key="swap",
                                   line_series=[("swap_used", COLORS["swap"])],
                                   line_max=0, line_fmt="{:.0f}%",
                                   desc=f"スワップ領域: {swap_total_g:.1f}GB\nディスク上の仮想メモリ (多用はメモリ不足の兆候)")

        # ─── Temperature ──────────────────────────────────
        if temp_data or nvidia_data or amd_data or gaudi_data:
            all_temps: list[float] = []
            for d in temp_data:
                if d.category == "DDR" and len(d.sensors) > 1:
                    all_temps.extend(s.temp_c for s in d.sensors)
                else:
                    all_temps.append(d.primary_temp_c)
            all_temps += [g.temperature_c for g in nvidia_data]
            all_temps += [g.temperature_c for g in amd_data if g.temperature_c > 0]
            all_temps += [d.temperature_c for d in gaudi_data if d.temperature_c > 0]
            max_temp = max(all_temps, default=0)
            n_sensors = len(all_temps)
            # 履歴記録 — カテゴリ別最高温度を記録 (サマリーグラフ用)
            _cat_maxes: dict[str, float] = {}
            for dev in temp_data:
                cat = dev.category
                t = dev.primary_temp_c
                _cat_maxes[cat] = max(_cat_maxes.get(cat, 0.0), t)
                # DDR: 各DIMM個別に記録
                if cat == "DDR" and len(dev.sensors) > 1:
                    for sens in dev.sensors:
                        slabel = sens.label.replace("TEMP_", "")
                        self._record(f"temp_DDR_{slabel}", sens.temp_c)
                        _cat_maxes[cat] = max(_cat_maxes.get(cat, 0.0), sens.temp_c)
            for cat, t in _cat_maxes.items():
                self._record(f"temp_{cat}", t)
            for g in nvidia_data:
                self._record(f"temp_GPU{g.index}", g.temperature_c)
            summary = f"Max:{self._fmt_temp(max_temp)}  {n_sensors} sensors"
            # temp_max を記録 (サマリー用)
            self._record("temp_max", max_temp)
            if sm and "temp" not in se:
                # CPU, GPU, MB, MEM の各カテゴリ最高温度を降順で
                _items: list[tuple[float, str, str, str]] = []  # (temp, label, hkey, color)
                cpu_devs = [d for d in temp_data if d.category == "CPU"]
                if cpu_devs:
                    hot = max(cpu_devs, key=lambda d: d.primary_temp_c)
                    _items.append((hot.primary_temp_c, "CPU", "temp_CPU", COLORS["user"]))
                gpu_temps = [(g.temperature_c, g.index) for g in nvidia_data] + \
                            [(g.temperature_c, g.index) for g in amd_data if g.temperature_c > 0]
                if gpu_temps:
                    t, idx = max(gpu_temps, key=lambda x: x[0])
                    _items.append((t, "GPU", f"temp_GPU{idx}", COLORS["warn"]))
                mb_devs = [d for d in temp_data if d.category == "Mainboard"]
                if mb_devs:
                    hot = max(mb_devs, key=lambda d: d.primary_temp_c)
                    _items.append((hot.primary_temp_c, "MB", "temp_Mainboard", COLORS["iowait"]))
                ddr_devs = [d for d in temp_data if d.category == "DDR"]
                if ddr_devs:
                    hot = max(ddr_devs, key=lambda d: d.primary_temp_c)
                    _items.append((hot.primary_temp_c, "MEM", "temp_DDR", COLORS["cache"]))
                nvme_devs = [d for d in temp_data if d.category == "NVMe"]
                if nvme_devs:
                    hot = max(nvme_devs, key=lambda d: d.primary_temp_c)
                    _items.append((hot.primary_temp_c, "NVMe", "temp_NVMe", COLORS["gpu_temp"]))
                # 温度降順ソート
                _items.sort(key=lambda x: -x[0])
                if not _items:
                    _items.append((max_temp, "Max", "temp_max", COLORS["gpu_temp"]))
                temp_series = [(hk, clr) for _, _, hk, clr in _items]
                temp_vals = [f"{lbl}:{self._fmt_temp(t)}" for t, lbl, _, _ in _items]
                y = self._draw_summary_row(y, "🌡TEMP",
                                           temp_series,
                                           f"Max:{self._fmt_temp(max_temp)} ({n_sensors})",
                                           max_val=-1, fmt_fn=self._fmt_temp_line,
                                           section="temp",
                                           values=temp_vals if len(temp_vals) > 1 else None,
                                           legend=[v.split(":")[0] for v in temp_vals] if len(temp_vals) <= 1 else None)
            else:
                y = self._draw_section_header(y, "temp", "Temperature", summary)
                self._current_section = "temp"
            if (not sm and self.expanded.get("temp", True)) or "temp" in se:
                _cat_desc = {
                    "CPU": "CPUパッケージ温度センサー",
                    "NVMe": "NVMe SSD コントローラー温度",
                    "Disk": "ディスクドライブ温度",
                    "GPU": "GPUチップ温度",
                    "ACPI": "ACPI サーマルゾーン (OS管理の温度監視)",
                    "Mainboard": "マザーボード温度 (チップセット/VRM周辺)",
                    "VRM": "VRM (電圧レギュレータ) 温度\nCPU/GPUへの電力供給回路",
                    "DDR": "DDR メモリモジュール温度\nDIMM上のSPDセンサー",
                    "WiFi": "WiFi モジュール温度",
                    "Thinkpad": "ThinkPad EC温度センサー",
                }
                for dev in temp_data:
                    # DDR: 各DIMM(センサー)を個別バーで表示
                    if dev.category == "DDR" and len(dev.sensors) > 1:
                        for si, sens in enumerate(dev.sensors):
                            temp = sens.temp_c
                            hw_crit = sens.crit_c
                            hw_max = sens.max_c
                            crit = hw_crit if hw_crit > 0 else 100.0
                            frac = min(temp / crit, 1.0)
                            if hw_crit > 0 and temp >= hw_crit:
                                color = COLORS["gpu_temp"]
                            elif hw_max > 0 and temp >= hw_max:
                                color = COLORS["warn"]
                            elif hw_crit > 0 and temp >= hw_crit * 0.8:
                                color = COLORS["warn"]
                            else:
                                color = COLORS["user"]
                            # ラベル: "TEMP_DDR5_A1" → "DDR5_A1"
                            slabel = sens.label.replace("TEMP_", "")
                            tk = f"temp_DDR_{slabel}"
                            self._record(tk, temp)
                            val = self._fmt_temp(temp, crit)
                            _desc = f"DDR メモリモジュール温度\n{sens.label}"
                            _desc += f"\nドライバ: {dev.name}"
                            y = self._draw_bar(y, f"🧩{slabel}"[:16],
                                               [(frac, color)], val,
                                               label_width=120,
                                               line_key=tk,
                                               line_series=[(tk, color)],
                                               line_max=-1, line_fmt_fn=self._fmt_temp_line,
                                               desc=_desc)
                        continue
                    # その他: 従来通り primary_temp_c
                    temp = dev.primary_temp_c
                    hw_crit = dev.primary_crit_c
                    hw_max = dev.primary_max_c
                    crit = hw_crit if hw_crit > 0 else 100.0
                    frac = min(temp / crit, 1.0)
                    if hw_crit > 0 and temp >= hw_crit:
                        color = COLORS["gpu_temp"]
                    elif hw_max > 0 and temp >= hw_max:
                        color = COLORS["warn"]
                    elif hw_crit > 0 and temp >= hw_crit * 0.9:
                        color = COLORS["warn"]
                    else:
                        color = COLORS["user"]
                    val = self._fmt_temp(temp, crit)
                    tk = f"temp_{dev.category}_{dev.device_label}" if dev.device_label else f"temp_{dev.category}"
                    self._record(tk, temp)
                    _desc = _cat_desc.get(dev.category, f"{dev.category} 温度センサー")
                    _desc += f"\nドライバ: {dev.name}"
                    if dev.device_label:
                        _desc += f"  デバイス: {dev.device_label}"
                    if hw_crit > 0:
                        _desc += f"\nCritical: {hw_crit:.0f}°C"
                    if hw_max > 0:
                        _desc += f"  Max: {hw_max:.0f}°C"
                    y = self._draw_bar(y, f"🌡{dev.display_name}"[:16],
                                       [(frac, color)], val,
                                       label_width=120,
                                       line_key=tk,
                                       line_series=[(tk, color)],
                                       line_max=-1, line_fmt_fn=self._fmt_temp_line,
                                       desc=_desc)
                # ファンセンサー (CPU除外)
                for dev in temp_data:
                    for fi, fan in enumerate(dev.fans):
                        max_rpm = 5000.0
                        frac = min(fan.rpm / max_rpm, 1.0) if max_rpm > 0 else 0.0
                        tfk = f"tfan_{dev.category}_{fi}"
                        self._record(tfk, fan.rpm)
                        y = self._draw_bar(y, f"🌀💨{fan.label}"[:12],
                                           [(frac, COLORS["gpu_fan"])],
                                           f"{fan.rpm} RPM",
                                           line_key=tfk,
                                           line_series=[(tfk, COLORS["gpu_fan"])],
                                           line_max=0, line_fmt="{:.0f}")
                for g in nvidia_data:
                    t_max = g.temp_max_c or 100.0
                    frac = min(g.temperature_c / t_max, 1.0)
                    color = self._gpu_temp_color(g.temperature_c, g)
                    tk = f"temp_GPU{g.index}"
                    y = self._draw_bar(y, f"🎮🌡GPU{g.index}",
                                       [(frac, color)], self._fmt_temp(g.temperature_c, g.temp_max_c),
                                       line_key=tk,
                                       line_series=[(tk, color)],
                                       line_max=-1, line_fmt_fn=self._fmt_temp_line)
                    if g.fan_speed_pct >= 0:
                        fan_frac = min(g.fan_speed_pct / 100.0, 1.0)
                        fk = f"nv{g.index}_fan"
                        self._record(fk, g.fan_speed_pct)
                        y = self._draw_bar(y, f"🎮💨FAN{g.index}",
                                           [(fan_frac, COLORS["gpu_fan"])],
                                           f"{g.fan_speed_pct:.0f}%",
                                           line_key=fk,
                                           line_series=[(fk, COLORS["gpu_fan"])],
                                           line_max=0, line_fmt="{:.0f}%")
                for g in amd_data:
                    if g.temperature_c > 0:
                        frac = min(g.temperature_c / 100.0, 1.0)
                        color = self._gpu_temp_color(g.temperature_c, g)
                        atk = f"temp_AMD{g.index}"
                        self._record(atk, g.temperature_c)
                        y = self._draw_bar(y, f"🎮🌡AMD{g.index}",
                                           [(frac, color)], self._fmt_temp(g.temperature_c),
                                           line_key=atk,
                                           line_series=[(atk, color)],
                                           line_max=-1, line_fmt_fn=self._fmt_temp_line)
                for d in gaudi_data:
                    if d.temperature_c > 0:
                        frac = min(d.temperature_c / 100.0, 1.0)
                        color = self._gpu_temp_color(d.temperature_c, d)
                        gtk = f"temp_HL{d.index}"
                        self._record(gtk, d.temperature_c)
                        y = self._draw_bar(y, f"🧮🌡HL{d.index}",
                                           [(frac, color)], self._fmt_temp(d.temperature_c),
                                           line_key=gtk,
                                           line_series=[(gtk, color)],
                                           line_max=-1, line_fmt_fn=self._fmt_temp_line)

        # ─── Disk I/O ─────────────────────────────────────
        if disk_data:
            total_r = sum(d.read_bytes_sec for d in disk_data)
            total_w = sum(d.write_bytes_sec for d in disk_data)
            # 自動スケール: 現在のピーク値を追跡 (ゆっくり減衰)
            cur_disk_peak = max(max((d.read_bytes_sec for d in disk_data), default=0),
                                max((d.write_bytes_sec for d in disk_data), default=0))
            if cur_disk_peak > self._peak_disk_bps:
                self._peak_disk_bps = cur_disk_peak
            else:
                self._peak_disk_bps = max(self._peak_disk_bps * 0.95, cur_disk_peak, 1_000.0)
            disk_scale = self._peak_disk_bps * 1.2  # 20% headroom
            # 個別ディスク履歴記録
            for d in disk_data:
                self._record(f"disk_{d.name}_R", d.read_bytes_sec)
                self._record(f"disk_{d.name}_W", d.write_bytes_sec)
            summary = f"R:{_fmt_bytes_sec(total_r)} W:{_fmt_bytes_sec(total_w)} [{_fmt_bytes_sec(disk_scale)}]"
            self._record("disk_total_R", total_r)
            self._record("disk_total_W", total_w)
            if sm and "disk" not in se:
                y = self._draw_summary_row(y, "💾DISK",
                                           [("disk_total_R", COLORS["cache"]),
                                            ("disk_total_W", COLORS["iowait"])],
                                           f"R:{_fmt_bytes_sec(total_r)} W:{_fmt_bytes_sec(total_w)}",
                                           max_val=0, legend=["read", "write"], section="disk")
            else:
                y = self._draw_section_header(y, "disk", f"Disk I/O ({len(disk_data)} devs)", summary)
                self._current_section = "disk"
            if (not sm and self.expanded["disk"]) or "disk" in se:
                show_raid = self.expanded.get("raid_members", False)
                for d in disk_data:
                    segs = [(min(d.read_bytes_sec / disk_scale, 0.5), COLORS["cache"]),
                            (min(d.write_bytes_sec / disk_scale, 0.5), COLORS["iowait"])]
                    val = f"R:{_fmt_bytes_sec(d.read_bytes_sec)} W:{_fmt_bytes_sec(d.write_bytes_sec)}"
                    dk = f"disk_{d.name}"
                    ls = [(f"{dk}_R", COLORS["cache"]), (f"{dk}_W", COLORS["iowait"])]

                    if d.raid_level:
                        y = self._draw_toggle_row(
                            y, "raid_members",
                            f"💾{d.display_name.upper()}", segs, val,
                            line_key=dk, line_series=ls, line_max=0,
                            line_fmt_fn=_fmt_bytes_sec)
                    elif d.raid_member_of:
                        if show_raid:
                            y = self._draw_bar(y, f" └💾{d.name}", segs, val,
                                               line_key=dk, line_series=ls, line_max=0,
                                               line_fmt_fn=_fmt_bytes_sec)
                    else:
                        y = self._draw_bar(y, f"💾{d.display_name.upper()}", segs, val,
                                           line_key=dk, line_series=ls, line_max=0,
                                           line_fmt_fn=_fmt_bytes_sec)

        # ─── Network ──────────────────────────────────────
        if net_data:
            total_rx = sum(n.rx_bytes_sec for n in net_data)
            total_tx = sum(n.tx_bytes_sec for n in net_data)
            # 自動スケール
            cur_net_peak = max(max((n.rx_bytes_sec for n in net_data), default=0),
                               max((n.tx_bytes_sec for n in net_data), default=0))
            if cur_net_peak > self._peak_net_bps:
                self._peak_net_bps = cur_net_peak
            else:
                self._peak_net_bps = max(self._peak_net_bps * 0.95, cur_net_peak, 1_000.0)
            net_scale = self._peak_net_bps * 1.2
            # 個別インターフェース履歴記録
            for n in net_data:
                self._record(f"net_{n.name}_rx", n.rx_bytes_sec)
                self._record(f"net_{n.name}_tx", n.tx_bytes_sec)
            summary = f"D:{_fmt_bytes_sec(total_rx)} U:{_fmt_bytes_sec(total_tx)} [{_fmt_bytes_sec(net_scale)}]"
            self._record("net_total_rx", total_rx)
            self._record("net_total_tx", total_tx)
            if sm and "network" not in se:
                y = self._draw_summary_row(y, "🌐NET",
                                           [("net_total_rx", COLORS["net_rx"]),
                                            ("net_total_tx", COLORS["net_tx"])],
                                           f"D:{_fmt_bytes_sec(total_rx)} U:{_fmt_bytes_sec(total_tx)}",
                                           max_val=0, legend=["down", "up"], section="network")
            else:
                y = self._draw_section_header(y, "network", "Network", summary)
                self._current_section = "network"
            if (not sm and self.expanded["network"]) or "network" in se:
                show_bond = self.expanded.get("bond_members", False)
                for n in net_data:
                    tag = n.net_type.value if hasattr(n, "net_type") else "???"
                    segs = [(min(n.rx_bytes_sec / net_scale, 0.5), COLORS["net_rx"]),
                            (min(n.tx_bytes_sec / net_scale, 0.5), COLORS["net_tx"])]
                    val = f"D:{_fmt_bytes_sec(n.rx_bytes_sec)} U:{_fmt_bytes_sec(n.tx_bytes_sec)}"
                    nk = f"net_{n.name}"
                    ls = [(f"{nk}_rx", COLORS["net_rx"]), (f"{nk}_tx", COLORS["net_tx"])]

                    if n.bond_mode:
                        y = self._draw_toggle_row(
                            y, "bond_members",
                            f"🌐{n.display_name}", segs, val,
                            line_key=nk, line_series=ls, line_max=0,
                            line_fmt_fn=_fmt_bytes_sec)
                    elif n.bond_member_of:
                        if show_bond:
                            y = self._draw_bar(y, f" └🌐{n.name}", segs, val,
                                               line_key=nk, line_series=ls, line_max=0,
                                               line_fmt_fn=_fmt_bytes_sec)
                    else:
                        y = self._draw_bar(y, f"🌐{tag} {n.name}", segs, val,
                                           line_key=nk, line_series=ls, line_max=0,
                                           line_fmt_fn=_fmt_bytes_sec)

        # ─── NFS ──────────────────────────────────────────
        if nfs_data:
            # 自動スケール
            cur_nfs_peak = max(max((m.read_bytes_sec for m in nfs_data), default=0),
                               max((m.write_bytes_sec for m in nfs_data), default=0))
            if cur_nfs_peak > self._peak_nfs_bps:
                self._peak_nfs_bps = cur_nfs_peak
            else:
                self._peak_nfs_bps = max(self._peak_nfs_bps * 0.95, cur_nfs_peak, 1_000.0)
            nfs_scale = self._peak_nfs_bps * 1.2
            # 個別マウント履歴記録
            for mt in nfs_data:
                mk = mt.mount_point.replace("/", "_")
                self._record(f"nfs{mk}_R", mt.read_bytes_sec)
                self._record(f"nfs{mk}_W", mt.write_bytes_sec)
            summary = f"{len(nfs_data)} mounts [{_fmt_bytes_sec(nfs_scale)}]"
            if sm and "nfs" not in se:
                mt0 = nfs_data[0]
                mk0 = mt0.mount_point.replace("/", "_")
                y = self._draw_summary_row(y, "📁NFS",
                                           [(f"nfs{mk0}_R", COLORS["cache"]),
                                            (f"nfs{mk0}_W", COLORS["iowait"])],
                                           f"{len(nfs_data)} mounts", max_val=0,
                                           legend=["read", "write"], section="nfs")
            else:
                y = self._draw_section_header(y, "nfs", "NFS/SAN/NAS", summary)
                self._current_section = "nfs"
            if (not sm and self.expanded["nfs"]) or "nfs" in se:
                for mt in nfs_data:
                    mk = mt.mount_point.replace("/", "_")
                    nk = f"nfs{mk}"
                    y = self._draw_bar(y, f"📁{mt.type_label} {mt.mount_point}"[:16],
                                       [(min(mt.read_bytes_sec / nfs_scale, 0.5), COLORS["net_rx"]),
                                        (min(mt.write_bytes_sec / nfs_scale, 0.5), COLORS["net_tx"])],
                                       f"R:{_fmt_bytes_sec(mt.read_bytes_sec)} W:{_fmt_bytes_sec(mt.write_bytes_sec)}",
                                       line_key=nk,
                                       line_series=[(f"{nk}_R", COLORS["net_rx"]),
                                                    (f"{nk}_W", COLORS["net_tx"])],
                                       line_max=0, line_fmt_fn=_fmt_bytes_sec)

        # ─── PCIe ─────────────────────────────────────────
        if pcie_data:
            # 自動スケール
            io_devs = [d for d in pcie_data if d.io_label]
            if io_devs:
                cur_pcie_peak = max(max((d.io_read_bytes_sec for d in io_devs), default=0),
                                    max((d.io_write_bytes_sec for d in io_devs), default=0))
                if cur_pcie_peak > self._peak_pcie_bps:
                    self._peak_pcie_bps = cur_pcie_peak
                else:
                    self._peak_pcie_bps = max(self._peak_pcie_bps * 0.95, cur_pcie_peak, 1_000.0)
            pcie_scale = self._peak_pcie_bps * 1.2
            # 個別デバイス履歴記録
            for d in pcie_data:
                if d.io_label:
                    pk = f"pcie_{d.short_name}"
                    self._record(f"{pk}_R", d.io_read_bytes_sec)
                    self._record(f"{pk}_W", d.io_write_bytes_sec)
            summary = f"{len(pcie_data)} devices [{_fmt_bytes_sec(pcie_scale)}]"
            if sm and "pcie" not in se:
                if io_devs:
                    pk0 = f"pcie_{io_devs[0].short_name}"
                    y = self._draw_summary_row(y, "PCIe",
                                               [(f"{pk0}_R", COLORS["cache"]),
                                                (f"{pk0}_W", COLORS["iowait"])],
                                               f"{len(pcie_data)} devs", max_val=0,
                                               legend=["read", "write"], section="pcie")
            else:
                y = self._draw_section_header(y, "pcie", "PCIe Devices", summary)
                self._current_section = "pcie"
            if (not sm and self.expanded["pcie"]) or "pcie" in se:
                for d in pcie_data:
                    icon = d.icon
                    link = f"{d.gen_name} x{d.current_width}"
                    if d.io_label:
                        bar_label = f"{icon}{d.io_label}" if icon else d.io_label
                        pk = f"pcie_{d.short_name}"
                        y = self._draw_bar(y, bar_label,
                                           [(min(d.io_read_bytes_sec / pcie_scale, 0.5), COLORS["cache"]),
                                            (min(d.io_write_bytes_sec / pcie_scale, 0.5), COLORS["iowait"])],
                                           f"{link} R:{_fmt_bytes_sec(d.io_read_bytes_sec)} W:{_fmt_bytes_sec(d.io_write_bytes_sec)}",
                                           label_width=120,
                                           line_key=pk,
                                           line_series=[(f"{pk}_R", COLORS["cache"]),
                                                        (f"{pk}_W", COLORS["iowait"])],
                                           line_max=0, line_fmt="{:.0f}",
                                           line_fmt_fn=_fmt_bytes_sec)
                    else:
                        dev_name = d.io_label or d.address
                        label = f"{icon} {dev_name}" if icon else dev_name
                        pk = f"pcie_{dev_name}"
                        y = self._draw_text(y,
                            f"{label:<20s} {d.short_name[:20]:<20s} {link} {d.current_bandwidth_gbs:5.1f} GB/s",
                            COLORS["pcie"], hide_key=pk)

        # ─── NVIDIA GPU ───────────────────────────────────
        if nvidia_data:
            for g in nvidia_data:
                self._record(f"gpu{g.index}_util", g.gpu_util_pct)
                self._record(f"gpu{g.index}_mem", g.mem_used_pct)
                self._record(f"gpu{g.index}_temp", g.temperature_c)
                self._record(f"gpu{g.index}_power", g.power_draw_w)
            summary = "  ".join(f"GPU{g.index}:{g.gpu_util_pct:.0f}%" for g in nvidia_data)
            if sm and "nvidia" not in se:
                for g in nvidia_data:
                    gk = f"gpu{g.index}"
                    y = self._draw_summary_row(
                        y, f"🎮GPU{g.index}",
                        [(f"{gk}_util", COLORS["gpu_util"]),
                         (f"{gk}_mem", COLORS["gpu_mem"]),
                         (f"{gk}_temp", COLORS["gpu_temp"]),
                         (f"{gk}_power", COLORS["gpu_power"])],
                        "", max_val=0, section="nvidia",
                        values=[f"util:{g.gpu_util_pct:.0f}%",
                                f"vram:{g.mem_used_pct:.0f}%",
                                f"tmp:{self._fmt_temp(g.temperature_c)}",
                                f"power:{g.power_draw_w:.0f}W"])
            else:
                y = self._draw_section_header(y, "nvidia", "NVIDIA GPU", summary)
            self._current_section = "nvidia"
            if (not sm and self.expanded["nvidia"]) or "nvidia" in se:
                for g in nvidia_data:
                    gk = f"gpu{g.index}"
                    y = self._draw_text(y, f"GPU{g.index} {g.short_name}", COLORS["fg_data"])
                    y = self._draw_bar(y, "  🎮UTIL",
                                       [(g.gpu_util_pct / 100, COLORS["gpu_util"])],
                                       f"{g.gpu_util_pct:.0f}%",
                                       line_key=f"{gk}_util",
                                       line_series=[(f"{gk}_util", COLORS["gpu_util"])],
                                       line_max=0, line_fmt="{:.0f}%",
                                       desc=f"GPU{g.index} ({g.short_name}) コア使用率\nCUDA/Tensorコアのビジー率")
                    y = self._draw_bar(y, "  🎮VRAM",
                                       [(g.mem_used_pct / 100, COLORS["gpu_mem"])],
                                       f"{_fmt_mib(g.mem_used_mib)}/{_fmt_mib(g.mem_total_mib)}",
                                       line_key=f"{gk}_mem",
                                       line_series=[(f"{gk}_mem", COLORS["gpu_mem"])],
                                       line_max=0, line_fmt="{:.0f}%",
                                       desc=f"GPU{g.index} ビデオメモリ (VRAM) 使用量\n総容量: {_fmt_mib(g.mem_total_mib)}")
                    t_color = self._gpu_temp_color(g.temperature_c, g)
                    _tdesc = f"GPU{g.index} チップ温度"
                    if g.temp_max_c > 0:
                        _tdesc += f"\nMax Operating: {g.temp_max_c:.0f}°C"
                    if g.temp_slowdown_c > 0:
                        _tdesc += f"  Slowdown: {g.temp_slowdown_c:.0f}°C"
                    if g.temp_shutdown_c > 0:
                        _tdesc += f"  Shutdown: {g.temp_shutdown_c:.0f}°C"
                    y = self._draw_bar(y, "  🎮🌡TEMP",
                                       [(g.temperature_c / 100, t_color)],
                                       self._fmt_temp(g.temperature_c),
                                       line_key=f"{gk}_temp",
                                       line_series=[(f"{gk}_temp", t_color)],
                                       line_max=-1, line_fmt_fn=self._fmt_temp_line,
                                       desc=_tdesc)
                    y = self._draw_bar(y, "  🎮POWER",
                                       [(g.power_pct / 100, COLORS["gpu_power"])],
                                       f"{g.power_draw_w:.0f}/{g.power_limit_w:.0f}W",
                                       line_key=f"{gk}_power",
                                       line_series=[(f"{gk}_power", COLORS["gpu_power"])],
                                       line_max=0, line_fmt="{:.0f}W",
                                       desc=f"GPU{g.index} 消費電力 / 電力上限\n上限: {g.power_limit_w:.0f}W")
                    if g.fan_speed_pct >= 0:
                        fk = f"{gk}_fan"
                        self._record(fk, g.fan_speed_pct)
                        y = self._draw_bar(y, "  🎮💨FAN",
                                           [(g.fan_speed_pct / 100, COLORS["gpu_fan"])],
                                           f"{g.fan_speed_pct:.0f}%",
                                           line_key=fk,
                                           line_series=[(fk, COLORS["gpu_fan"])],
                                           line_max=0, line_fmt="{:.0f}%")

        # ─── AMD GPU ──────────────────────────────────────
        if amd_data:
            for g in amd_data:
                self._record(f"amd{g.index}_util", g.gpu_util_pct)
            summary = "  ".join(f"GPU{g.index}:{g.gpu_util_pct:.0f}%" for g in amd_data)
            if sm and "amd" not in se:
                for g in amd_data:
                    ak = f"amd{g.index}"
                    mem_str = f" {g.mem_used_pct:.0f}%" if g.mem_total_mib > 0 else ""
                    y = self._draw_summary_row(
                        y, f"🎮AMD{g.index}",
                        [(f"{ak}_util", COLORS["gpu_util"])],
                        f"{g.gpu_util_pct:.0f}%{mem_str}", max_val=0,
                        legend=["util"], section="amd")
            else:
                y = self._draw_section_header(y, "amd", "AMD GPU (ROCm)", summary)
            self._current_section = "amd"
            if (not sm and self.expanded["amd"]) or "amd" in se:
                for g in amd_data:
                    ak = f"amd{g.index}"
                    y = self._draw_text(y, f"GPU{g.index} {g.short_name}", COLORS["fg_data"])
                    y = self._draw_bar(y, "  🎮UTIL",
                                       [(g.gpu_util_pct / 100, COLORS["gpu_util"])],
                                       f"{g.gpu_util_pct:.0f}%",
                                       line_key=f"{ak}_util",
                                       line_series=[(f"{ak}_util", COLORS["gpu_util"])],
                                       line_max=0, line_fmt="{:.0f}%",
                                       desc=f"AMD GPU{g.index} ({g.short_name}) コア使用率\nROCm/rocm-smi で取得")
                    if g.mem_total_mib > 0:
                        self._record(f"{ak}_mem", g.mem_used_pct)
                        y = self._draw_bar(y, "  🎮VRAM",
                                           [(g.mem_used_pct / 100, COLORS["gpu_mem"])],
                                           f"{_fmt_mib(g.mem_used_mib)}/{_fmt_mib(g.mem_total_mib)}",
                                           line_key=f"{ak}_mem",
                                           line_series=[(f"{ak}_mem", COLORS["gpu_mem"])],
                                           line_max=0, line_fmt="{:.0f}%",
                                           desc=f"AMD GPU{g.index} ビデオメモリ (VRAM)\n総容量: {_fmt_mib(g.mem_total_mib)}")

        # ─── Intel Gaudi ──────────────────────────────────
        if gaudi_data:
            for d in gaudi_data:
                self._record(f"gaudi{d.index}_util", d.aip_util_pct)
            summary = "  ".join(f"HL{d.index}:{d.aip_util_pct:.0f}%" for d in gaudi_data)
            if sm and "gaudi" not in se:
                for d in gaudi_data:
                    gk = f"gaudi{d.index}"
                    mem_str = f" {d.mem_used_pct:.0f}%" if d.mem_total_mib > 0 else ""
                    y = self._draw_summary_row(
                        y, f"🧮HL{d.index}",
                        [(f"{gk}_util", COLORS["gpu_util"])],
                        f"{d.aip_util_pct:.0f}%{mem_str}", max_val=0,
                        legend=["aip"], section="gaudi")
            else:
                y = self._draw_section_header(y, "gaudi", "Intel Gaudi", summary)
            self._current_section = "gaudi"
            if (not sm and self.expanded["gaudi"]) or "gaudi" in se:
                for d in gaudi_data:
                    gk = f"gaudi{d.index}"
                    y = self._draw_text(y, f"HL{d.index} {d.short_name}", COLORS["fg_data"])
                    y = self._draw_bar(y, "  🧮AIP",
                                       [(d.aip_util_pct / 100, COLORS["gpu_util"])],
                                       f"{d.aip_util_pct:.0f}%",
                                       line_key=f"{gk}_util",
                                       line_series=[(f"{gk}_util", COLORS["gpu_util"])],
                                       line_max=0, line_fmt="{:.0f}%",
                                       desc=f"Gaudi HL{d.index} ({d.short_name}) AIP使用率\nAI Processing Unit のビジー率 (hl-smi)")
                    if d.mem_total_mib > 0:
                        self._record(f"{gk}_mem", d.mem_used_pct)
                        y = self._draw_bar(y, "  🧮HBM",
                                           [(d.mem_used_pct / 100, COLORS["gpu_mem"])],
                                           f"{_fmt_mib(d.mem_used_mib)}/{_fmt_mib(d.mem_total_mib)}",
                                           line_key=f"{gk}_mem",
                                           line_series=[(f"{gk}_mem", COLORS["gpu_mem"])],
                                           line_max=0, line_fmt="{:.0f}%",
                                           desc=f"Gaudi HL{d.index} HBM (High Bandwidth Memory)\n総容量: {_fmt_mib(d.mem_total_mib)}")

        # ─── GPU Processes ────────────────────────────────
        self._current_section = ""
        if not sm and gpu_proc_data:
            summary = f"{len(gpu_proc_data)} procs"
            y = self._draw_section_header(y, "gpu_proc", "GPU Processes", summary)
            if self.expanded["gpu_proc"]:
                for p in gpu_proc_data:
                    y = self._draw_text(y,
                        f"GPU{p.gpu_index}  PID:{p.pid:>7d}  {p.name:<18s}  VRAM:{p.gpu_mem_mib:7.0f} MiB",
                        COLORS["gpu_mem"])

        # ─── Top Processes ────────────────────────────────
        if not sm and proc_data:
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
        # 描画時間を計測
        t_draw_end = time.perf_counter()
        self._prof["_draw"] = (t_draw_end - t_draw_start) * 1000
        self._prof_total = self._prof.get("_collect", 0) + self._prof["_draw"]

        y += 6
        footer_h = 28
        self.canvas.create_rectangle(0, y, c_width, y + footer_h,
                                     fill=COLORS["header"], outline="")
        self.canvas.create_line(0, y, c_width, y,
                                fill=COLORS["header_line"], width=1)
        self.canvas.create_text(
            c_width // 2, y + footer_h // 2,
            text="Bar icon: toggle line | Click bar: hide | Header icon: all line/reset | s:summary | f:C/F | +/-:interval | q:quit",
            fill=COLORS["fg_sub"], font=("monospace", 9))
        y += footer_h

        # プロファイル表示
        prof_h = 16
        self.canvas.create_rectangle(0, y, c_width, y + prof_h,
                                     fill=COLORS["bg"], outline="")
        # 上位コスト順でコレクター時間を表示
        sorted_prof = sorted(
            ((k, v) for k, v in self._prof.items() if not k.startswith("_")),
            key=lambda x: -x[1])
        parts = [f"{k}:{v:.0f}" for k, v in sorted_prof if v >= 0.1]
        prof_text = (f"Frame:{self._prof_total:.0f}ms "
                     f"(collect:{self._prof.get('_collect', 0):.0f} "
                     f"draw:{self._prof.get('_draw', 0):.0f}) "
                     + " ".join(parts))
        # ログファイルに書き出し (毎秒)
        try:
            with open("/tmp/housekeeper_prof.log", "a") as f:
                f.write(prof_text + "\n")
        except OSError:
            pass
        self.canvas.create_text(
            10, y + prof_h // 2, anchor="w", text=prof_text,
            fill=COLORS["text_dim"], font=("monospace", 8))
        y += prof_h + 5

        # ヘルプオーバーレイ
        if self._show_help:
            self._draw_help_overlay(c_width)

        # 右クリックツールチップ
        self._draw_tooltip()

        # スクロール領域更新
        self.canvas.configure(scrollregion=(0, 0, c_width, y + 10))

    def _draw_help_overlay(self, c_width: int) -> None:
        """画面中央にヘルプオーバーレイを描画。"""
        c = self.canvas
        c_height = c.winfo_height() or 900

        # 半透明風の背景 (暗いオーバーレイ)
        c.create_rectangle(0, 0, c_width, c_height,
                           fill="#000000", stipple="gray50", outline="")

        # ヘルプボックス
        help_lines = [
            "── housekeeper ──",
            "",
            "Bar left icon          Toggle line chart",
            "Click bar/line         Hide it",
            "Header left icon       All line / reset",
            "Click section header   Expand / Collapse",
            "Click RAID/Bond row    Show / Hide members",
            "Click  ?  button       Show this help",
            "",
            "s                      Toggle summary mode",
            "f                      Toggle °C / °F",
            "+  /  -                Change update interval",
            "q  /  Esc              Quit",
            "h                      Toggle this help",
            "",
            "Click anywhere to close",
        ]
        box_w = 380
        line_h = 22
        box_h = len(help_lines) * line_h + 40
        bx = (c_width - box_w) // 2
        by = (c_height - box_h) // 2

        # ボックス背景 + ボーダー
        c.create_rectangle(bx, by, bx + box_w, by + box_h,
                           fill=COLORS["header"], outline=COLORS["fg"], width=2)
        # 上下オレンジライン
        c.create_line(bx, by + 1, bx + box_w, by + 1,
                      fill=COLORS["header_line"], width=2)
        c.create_line(bx, by + box_h - 1, bx + box_w, by + box_h - 1,
                      fill=COLORS["header_line"], width=2)

        # テキスト
        ty = by + 20
        for line in help_lines:
            if line.startswith("──"):
                c.create_text(bx + box_w // 2, ty,
                              text=line, fill=COLORS["fg"],
                              font=("monospace", 13, "bold"))
            elif line == "":
                pass  # 空行
            elif line.startswith("Click anywhere"):
                c.create_text(bx + box_w // 2, ty,
                              text=line, fill=COLORS["fg_sub"],
                              font=("monospace", 9, "italic"))
            else:
                # 左側 (操作) と右側 (説明) を分割
                parts = line.split(None, 1)
                # 固定幅で左右に分ける
                left = line[:23].rstrip()
                right = line[23:].strip()
                c.create_text(bx + 20, ty, anchor="w",
                              text=left, fill=COLORS["fg"],
                              font=("monospace", 11, "bold"))
                c.create_text(bx + 210, ty, anchor="w",
                              text=right, fill=COLORS["fg_data"],
                              font=("monospace", 11))
            ty += line_h

    def run(self) -> None:
        self.root.after(500, self._update)
        self.root.mainloop()


def run_gui(args: argparse.Namespace) -> None:
    """X11 GUI を起動するエントリポイント。"""
    app = HousekeeperGui(args)
    app.run()
