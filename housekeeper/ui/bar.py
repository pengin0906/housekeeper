"""xosview-style horizontal bar meter renderer.

バーの描画方式:
  [ラベル] [========----        ] [値テキスト]

セグメントごとに異なる色で塗り分ける (CPU の user/system/idle など)。
Unicode ブロック文字 (▏▎▍▌▋▊▉█) でサブピクセル精度を実現。
"""

from __future__ import annotations

import curses
from dataclasses import dataclass


# Unicode ブロック文字 (1/8 〜 8/8)
_BLOCKS = " ▏▎▍▌▋▊▉█"


@dataclass
class BarSegment:
    """バーの1セグメント。"""
    fraction: float      # 0.0〜1.0
    color_pair: int      # curses color pair ID
    char: str = "█"      # 塗りつぶし文字


def draw_bar(
    win: curses.window,
    y: int,
    x: int,
    width: int,
    segments: list[BarSegment],
    label: str = "",
    label_width: int = 10,
    value_text: str = "",
    value_width: int = 8,
    label_color: int = 0,
    value_color: int = 0,
) -> None:
    """1行のバーメーターを描画する。

    Parameters
    ----------
    win : curses window
    y, x : 描画開始位置
    width : バー全体の幅 (ラベル・値含む)
    segments : BarSegment のリスト (合計 fraction <= 1.0)
    label : 左側ラベル
    label_width : ラベル領域の幅
    value_text : 右側の値テキスト
    value_width : 値領域の幅
    """
    max_y, max_x = win.getmaxyx()
    if y >= max_y - 1 or x >= max_x:
        return

    # ラベル描画
    try:
        lbl = label[:label_width].ljust(label_width)
        # addnstr の n はバイト数として扱われるため encode 後の長さを渡す
        lbl_bytes = len(lbl.encode())
        win.addnstr(y, x, lbl, min(lbl_bytes, max_x - x),
                     curses.color_pair(label_color) | curses.A_BOLD)
    except curses.error:
        pass

    bar_x = x + label_width + 1
    bar_width = width - label_width - value_width - 2  # 余白分

    if bar_width < 1:
        return

    # バーの背景 (空の部分)
    try:
        bg = "░" * bar_width
        win.addnstr(y, bar_x, bg, min(bar_width, max_x - bar_x),
                     curses.color_pair(0) | curses.A_DIM)
    except curses.error:
        pass

    # セグメント描画
    pos = 0.0
    for seg in segments:
        if seg.fraction <= 0:
            continue
        seg_width_f = seg.fraction * bar_width
        start_col = int(pos)
        end_col_f = pos + seg_width_f
        full_cols = int(end_col_f) - start_col
        remainder = end_col_f - int(end_col_f)

        # フルブロック文字
        if full_cols > 0 and bar_x + start_col < max_x:
            txt = seg.char * min(full_cols, max_x - bar_x - start_col)
            try:
                win.addnstr(y, bar_x + start_col, txt, len(txt),
                             curses.color_pair(seg.color_pair) | curses.A_BOLD)
            except curses.error:
                pass

        # サブピクセル (部分ブロック文字)
        partial_col = start_col + full_cols
        if remainder > 0.125 and bar_x + partial_col < max_x:
            block_idx = int(remainder * 8)
            block_idx = max(1, min(block_idx, 8))
            try:
                win.addnstr(y, bar_x + partial_col, _BLOCKS[block_idx], 1,
                             curses.color_pair(seg.color_pair))
            except curses.error:
                pass

        pos += seg_width_f

    # 値テキスト描画 (右端)
    val_x = x + width - value_width
    if val_x < max_x:
        try:
            win.addnstr(y, val_x, value_text[:value_width].rjust(value_width),
                         min(value_width, max_x - val_x),
                         curses.color_pair(value_color))
        except curses.error:
            pass


def draw_section_header(
    win: curses.window,
    y: int,
    x: int,
    width: int,
    title: str,
    color_pair: int = 0,
) -> None:
    """セクションヘッダーを描画 (タイトル + 水平線)。"""
    max_y, max_x = win.getmaxyx()
    if y >= max_y - 1:
        return

    try:
        header = f"─── {title} "
        header += "─" * max(0, width - len(header))
        win.addnstr(y, x, header[:max_x - x], max_x - x,
                     curses.color_pair(color_pair) | curses.A_BOLD)
    except curses.error:
        pass
