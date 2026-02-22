"""Process collector - /proc を読み取ってトッププロセスを取得。

/proc/[pid]/stat と /proc/[pid]/cmdline から:
  - CPU 使用率の高いプロセス
  - メモリ使用量の高いプロセス
を取得する。

Claude Code, antigravity, python, node 等のプロセスを特定して
わかりやすい名前で表示する。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcessInfo:
    """プロセス情報。"""
    pid: int
    name: str            # わかりやすいプロセス名
    cmdline: str         # フルコマンドライン
    cpu_pct: float = 0.0
    mem_rss_kb: int = 0
    state: str = ""      # R, S, D, Z, ...

    @property
    def mem_rss_mib(self) -> float:
        return self.mem_rss_kb / 1024


# 既知アプリのマッチング (python/node は除く、スクリプト名を優先するため)
_APP_NAMES: list[tuple[str, str]] = [
    ("claude", "Claude Code"),
    ("antigravity", "Antigravity"),
    ("ollama", "Ollama"),
    ("vllm", "vLLM"),
    ("tritonserver", "Triton Server"),
    ("torchrun", "PyTorch DDP"),
    ("deepspeed", "DeepSpeed"),
    ("jupyter", "Jupyter"),
    ("nvcc", "CUDA Compiler"),
    ("code-server", "VS Code Server"),
    ("code ", "VS Code"),       # "code " (末尾スペース) で部分マッチ回避
    ("docker", "Docker"),
    ("containerd", "containerd"),
    ("npm ", "npm"),
    ("npx ", "npx"),
]


def _get_friendly_name(cmdline: str, comm: str) -> str:
    """コマンドラインからフレンドリー名を推定する。

    "python train.py --lr 0.01" → "py:train"
    "python -m torch.distributed.launch" → "py:torch"
    "/usr/bin/claude" → "Claude Code"
    """
    cmdline_lower = cmdline.lower()
    parts = cmdline.split()
    if not parts:
        return comm or "unknown"

    base = os.path.basename(parts[0])

    # python/python3: スクリプト名や -m モジュール名を最優先で取得
    if base.startswith("python"):
        # まず cmdline 内の既知アプリ名を確認 (vllm, deepspeed等)
        for key, friendly in _APP_NAMES:
            if key in cmdline_lower:
                return friendly
        # -m module.name
        for i, p in enumerate(parts):
            if p == "-m" and i + 1 < len(parts):
                mod = parts[i + 1]
                return f"py:{mod.split('.')[0]}"
        # script.py
        for p in parts[1:]:
            if not p.startswith("-"):
                script = os.path.basename(p)
                if script.endswith(".py"):
                    return f"py:{script[:-3]}"
                return f"py:{script}"
        return "Python"

    # node/nodejs
    if base in ("node", "nodejs"):
        for key, friendly in _APP_NAMES:
            if key in cmdline_lower:
                return friendly
        for p in parts[1:]:
            if not p.startswith("-"):
                return f"node:{os.path.basename(p)}"
        return "Node.js"

    # その他の既知アプリ
    for key, friendly in _APP_NAMES:
        if key in cmdline_lower:
            return friendly

    return comm or base


@dataclass
class _ProcStat:
    pid: int
    comm: str
    state: str
    utime: int
    stime: int
    rss: int  # ページ数


class ProcessCollector:
    """プロセス情報コレクター。"""

    def __init__(self, top_n: int = 8) -> None:
        self.top_n = top_n
        self._prev: dict[int, _ProcStat] = {}
        self._prev_time: float = 0.0
        self._clk_tck = os.sysconf("SC_CLK_TCK")
        self._page_size = os.sysconf("SC_PAGE_SIZE")

    def _read_proc(self, pid: int) -> _ProcStat | None:
        try:
            stat_path = Path(f"/proc/{pid}/stat")
            stat_data = stat_path.read_text()

            # comm はカッコで囲まれている: "pid (comm) state ..."
            lparen = stat_data.index("(")
            rparen = stat_data.rindex(")")
            comm = stat_data[lparen + 1:rparen]
            rest = stat_data[rparen + 2:].split()

            state = rest[0]
            utime = int(rest[11])
            stime = int(rest[12])
            rss = int(rest[21])

            return _ProcStat(pid=pid, comm=comm, state=state,
                             utime=utime, stime=stime, rss=rss)
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            return None

    def _read_cmdline(self, pid: int) -> str:
        try:
            data = Path(f"/proc/{pid}/cmdline").read_text()
            return data.replace("\0", " ").strip()
        except (FileNotFoundError, PermissionError):
            return ""

    def collect(self) -> list[ProcessInfo]:
        now = time.monotonic()
        dt = now - self._prev_time if self._prev_time else 0.0

        curr: dict[int, _ProcStat] = {}
        processes: list[ProcessInfo] = []

        try:
            pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
        except OSError:
            return []

        for pid in pids:
            stat = self._read_proc(pid)
            if stat is None:
                continue
            curr[pid] = stat

            # CPU 使用率計算
            cpu_pct = 0.0
            if dt > 0 and pid in self._prev:
                prev = self._prev[pid]
                d_utime = stat.utime - prev.utime
                d_stime = stat.stime - prev.stime
                cpu_pct = 100.0 * (d_utime + d_stime) / (dt * self._clk_tck)

            mem_kb = stat.rss * self._page_size // 1024
            cmdline = self._read_cmdline(pid)
            friendly = _get_friendly_name(cmdline, stat.comm)

            processes.append(ProcessInfo(
                pid=pid,
                name=friendly,
                cmdline=cmdline,
                cpu_pct=cpu_pct,
                mem_rss_kb=mem_kb,
                state=stat.state,
            ))

        self._prev = curr
        self._prev_time = now

        # CPU 使用率でソートして上位 N 件
        processes.sort(key=lambda p: p.cpu_pct, reverse=True)
        return processes[:self.top_n]
