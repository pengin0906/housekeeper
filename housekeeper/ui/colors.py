"""Color definitions for the TUI.

curses の色ペアを初期化し、xosview 風の配色を提供する。
256色 / TrueColor 非対応ターミナルでもフォールバックする。
"""

from __future__ import annotations

import curses

# 色ペア ID (1〜始まり、0 はデフォルト)
PAIR_TITLE = 1
PAIR_LABEL = 2
PAIR_USER = 3       # CPU user / Memory used   - 緑
PAIR_NICE = 4       # CPU nice                  - 黄
PAIR_SYSTEM = 5     # CPU system                - 赤
PAIR_IOWAIT = 6     # CPU iowait / Disk         - マゼンタ
PAIR_IRQ = 7        # CPU irq+softirq           - 青
PAIR_IDLE = 8       # idle / free               - 暗い
PAIR_CACHE = 9      # cache / buffers           - シアン
PAIR_GPU_UTIL = 10  # GPU utilization           - 緑
PAIR_GPU_MEM = 11   # GPU memory                - 黄
PAIR_GPU_TEMP = 12  # GPU temperature           - 赤
PAIR_GPU_POWER = 13 # GPU power                 - マゼンタ
PAIR_NET_RX = 14    # Network RX                - シアン
PAIR_NET_TX = 15    # Network TX                - 緑
PAIR_STEAL = 16     # CPU steal                 - 白 on 赤
PAIR_SWAP = 17      # Swap used                 - 赤
PAIR_BAR_BG = 18    # バーの背景               - 暗い
PAIR_HEADER = 19    # セクションヘッダー        - ボールド白
PAIR_GPU_FAN = 20   # GPU fan speed             - シアン
PAIR_GPU_ENC = 21   # GPU encoder               - 青


def init_colors() -> None:
    """curses 色ペアを初期化する。has_colors() が False ならノーオプ。"""
    if not curses.has_colors():
        return

    curses.start_color()
    curses.use_default_colors()

    pairs = {
        PAIR_TITLE:     (curses.COLOR_WHITE, -1),
        PAIR_LABEL:     (curses.COLOR_WHITE, -1),
        PAIR_USER:      (curses.COLOR_GREEN, -1),
        PAIR_NICE:      (curses.COLOR_YELLOW, -1),
        PAIR_SYSTEM:    (curses.COLOR_RED, -1),
        PAIR_IOWAIT:    (curses.COLOR_MAGENTA, -1),
        PAIR_IRQ:       (curses.COLOR_BLUE, -1),
        PAIR_IDLE:      (curses.COLOR_BLACK, -1),
        PAIR_CACHE:     (curses.COLOR_CYAN, -1),
        PAIR_GPU_UTIL:  (curses.COLOR_GREEN, -1),
        PAIR_GPU_MEM:   (curses.COLOR_YELLOW, -1),
        PAIR_GPU_TEMP:  (curses.COLOR_RED, -1),
        PAIR_GPU_POWER: (curses.COLOR_MAGENTA, -1),
        PAIR_NET_RX:    (curses.COLOR_CYAN, -1),
        PAIR_NET_TX:    (curses.COLOR_GREEN, -1),
        PAIR_STEAL:     (curses.COLOR_WHITE, curses.COLOR_RED),
        PAIR_SWAP:      (curses.COLOR_RED, -1),
        PAIR_BAR_BG:    (curses.COLOR_BLACK, -1),
        PAIR_HEADER:    (curses.COLOR_WHITE, -1),
        PAIR_GPU_FAN:   (curses.COLOR_CYAN, -1),
        PAIR_GPU_ENC:   (curses.COLOR_BLUE, -1),
    }

    for pair_id, (fg, bg) in pairs.items():
        try:
            curses.init_pair(pair_id, fg, bg)
        except curses.error:
            pass
