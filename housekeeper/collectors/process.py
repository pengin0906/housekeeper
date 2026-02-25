"""Process collector - トッププロセスを取得。

Linux: /proc/[pid]/stat + /proc/[pid]/cmdline
macOS: ps -eo pid,pcpu,rss,comm
Windows: PowerShell Get-Process
"""

from __future__ import annotations

import os
import subprocess
import sys
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

_IS_DARWIN = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"


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
        if not _IS_WIN:
            try:
                self._clk_tck = os.sysconf("SC_CLK_TCK")
                self._page_size = os.sysconf("SC_PAGE_SIZE")
            except (ValueError, OSError):
                self._clk_tck = 100
                self._page_size = 4096
        else:
            self._clk_tck = 100
            self._page_size = 4096

    def _read_proc(self, pid: int) -> _ProcStat | None:
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat_data = f.read()

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
        except (FileNotFoundError, PermissionError, ValueError, IndexError, OSError):
            return None

    def _read_cmdline(self, pid: int) -> str:
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                data = f.read()
            return data.replace("\0", " ").strip()
        except (FileNotFoundError, PermissionError, OSError):
            return ""

    def collect(self) -> list[ProcessInfo]:
        if _IS_DARWIN:
            return self._collect_darwin()
        if _IS_WIN:
            return self._collect_win()
        return self._collect_linux()

    def _collect_linux(self) -> list[ProcessInfo]:
        import heapq

        now = time.monotonic()
        dt = now - self._prev_time if self._prev_time else 0.0
        inv_dt = 100.0 / (dt * self._clk_tck) if dt > 0 else 0.0
        page_kb = self._page_size >> 10  # //1024 を事前計算

        curr: dict[int, _ProcStat] = {}
        # (cpu_pct, pid, stat) のタプルで top-N を保持
        candidates: list[tuple[float, int, _ProcStat]] = []

        try:
            pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
        except OSError:
            return []

        prev = self._prev
        for pid in pids:
            stat = self._read_proc(pid)
            if stat is None:
                continue
            curr[pid] = stat

            # CPU 使用率計算
            cpu_pct = 0.0
            if inv_dt > 0:
                p = prev.get(pid)
                if p is not None:
                    cpu_pct = (stat.utime - p.utime + stat.stime - p.stime) * inv_dt

            candidates.append((cpu_pct, pid, stat))

        self._prev = curr
        self._prev_time = now

        # top-N を heapq で高速取得 (全件ソート不要)
        n = self.top_n if self.top_n > 0 else len(candidates)
        top = heapq.nlargest(n, candidates, key=lambda x: x[0])

        # top-N のみ cmdline を読む (ここが最大の節約)
        processes: list[ProcessInfo] = []
        for cpu_pct, pid, stat in top:
            cmdline = self._read_cmdline(pid)
            friendly = _get_friendly_name(cmdline, stat.comm)
            processes.append(ProcessInfo(
                pid=pid,
                name=friendly,
                cmdline=cmdline,
                cpu_pct=cpu_pct,
                mem_rss_kb=stat.rss * page_kb,
                state=stat.state,
            ))

        return processes

    def _collect_darwin(self) -> list[ProcessInfo]:
        """macOS: ps でプロセス情報を取得 (フルコマンドライン付き)。"""
        processes: list[ProcessInfo] = []
        try:
            # command= でフルコマンドライン (引数付き) を取得
            out = subprocess.run(
                ["ps", "-eo", "pid=,pcpu=,rss=,command="],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode != 0:
                return []
            for line in out.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                try:
                    pid = int(parts[0])
                    cpu_pct = float(parts[1])
                    rss_kb = int(parts[2])
                    cmdline = parts[3].strip()
                    comm = os.path.basename(cmdline.split()[0]) if cmdline else ""
                    name = _get_friendly_name(cmdline, comm)
                    processes.append(ProcessInfo(
                        pid=pid,
                        name=name,
                        cmdline=cmdline,
                        cpu_pct=cpu_pct,
                        mem_rss_kb=rss_kb,
                    ))
                except (ValueError, IndexError):
                    continue
        except (OSError, subprocess.TimeoutExpired):
            pass

        processes.sort(key=lambda p: p.cpu_pct, reverse=True)
        return processes if self.top_n <= 0 else processes[:self.top_n]

    def _collect_win(self) -> list[ProcessInfo]:
        """Windows: PowerShell でプロセス情報を取得。"""
        processes: list[ProcessInfo] = []
        try:
            cmd = (
                "Get-Process | Sort-Object CPU -Descending | "
                "Select-Object -First 15 | "
                "ForEach-Object { $_.Id.ToString() + '|' + "
                "[math]::Round($_.CPU,1).ToString() + '|' + "
                "([math]::Round($_.WorkingSet64/1KB,0)).ToString() + '|' + "
                "$_.ProcessName }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                for line in out.stdout.strip().splitlines():
                    parts = line.split("|", 3)
                    if len(parts) >= 4:
                        try:
                            pid = int(parts[0])
                            cpu_pct = float(parts[1]) if parts[1] else 0.0
                            rss_kb = int(float(parts[2])) if parts[2] else 0
                            comm = parts[3].strip()
                            name = _get_friendly_name(comm, comm)
                            processes.append(ProcessInfo(
                                pid=pid,
                                name=name,
                                cmdline=comm,
                                cpu_pct=cpu_pct,
                                mem_rss_kb=rss_kb,
                            ))
                        except (ValueError, IndexError):
                            continue
        except (OSError, subprocess.TimeoutExpired):
            pass

        processes.sort(key=lambda p: p.cpu_pct, reverse=True)
        return processes if self.top_n <= 0 else processes[:self.top_n]
