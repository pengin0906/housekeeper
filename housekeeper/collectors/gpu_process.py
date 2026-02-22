"""NVIDIA GPU プロセス情報コレクター。

nvidia-smi で GPU を使用しているプロセスの一覧を取得する。
どのプロセスがどの GPU でどれだけ VRAM を使用しているかがわかる。

nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory,name
           --format=csv,noheader,nounits
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GpuProcessInfo:
    """GPU を使用しているプロセスの情報。"""
    pid: int
    gpu_index: int
    name: str               # わかりやすいプロセス名
    cmdline: str
    gpu_mem_mib: float = 0.0

    @property
    def short_info(self) -> str:
        return f"PID:{self.pid} {self.name} {self.gpu_mem_mib:.0f}MiB"


def _read_cmdline(pid: int) -> str:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_text()
        return data.replace("\0", " ").strip()
    except (FileNotFoundError, PermissionError):
        return ""


def _friendly_name(cmdline: str, raw_name: str) -> str:
    """コマンドラインからフレンドリー名を推定。

    "python train.py --epochs 10" → "train.py"
    "python -m vllm.entrypoints.api_server" → "vLLM"
    "/usr/bin/ollama serve" → "Ollama"
    """
    cmdline_lower = cmdline.lower()

    # 既知のアプリケーション名マッチ (優先度順)
    name_map = [
        ("claude", "Claude Code"),
        ("antigravity", "Antigravity"),
        ("ollama", "Ollama"),
        ("vllm", "vLLM"),
        ("tritonserver", "Triton"),
        ("torchrun", "PyTorch DDP"),
        ("deepspeed", "DeepSpeed"),
        ("nemo", "NeMo"),
        ("jupyter", "Jupyter"),
        ("stable-diffusion", "SD"),
        ("comfyui", "ComfyUI"),
        ("text-generation", "TGI"),
        ("llama.cpp", "llama.cpp"),
        ("whisper", "Whisper"),
        ("diffusers", "Diffusers"),
        ("transformers", "HF Transformers"),
        ("accelerate", "HF Accelerate"),
        ("fairseq", "Fairseq"),
        ("megatron", "Megatron-LM"),
    ]

    for key, friendly in name_map:
        if key in cmdline_lower:
            return friendly

    # python/python3 で実行中のスクリプト名を取得
    parts = cmdline.split()
    if parts:
        base = os.path.basename(parts[0])
        if base in ("python", "python3", "python3.10", "python3.11", "python3.12"):
            # python -m module.name の場合
            for i, p in enumerate(parts):
                if p == "-m" and i + 1 < len(parts):
                    mod = parts[i + 1]
                    # "vllm.entrypoints.api_server" → "vllm"
                    top_mod = mod.split(".")[0]
                    return f"py:{top_mod}"
            # python script.py の場合
            for p in parts[1:]:
                if not p.startswith("-"):
                    script = os.path.basename(p)
                    if script.endswith(".py"):
                        return f"py:{script[:-3]}"
                    return f"py:{script}"
            return "Python"

        if base in ("node", "nodejs"):
            for p in parts[1:]:
                if not p.startswith("-"):
                    return f"node:{os.path.basename(p)}"
            return "Node.js"

    return os.path.basename(raw_name) if raw_name else "unknown"


class GpuProcessCollector:
    """GPU プロセスコレクター (NVIDIA)。"""

    def __init__(self) -> None:
        self._uuid_to_index: dict[str, int] = {}

    def available(self) -> bool:
        return bool(shutil.which("nvidia-smi"))

    def _build_uuid_map(self) -> None:
        """GPU UUID → インデックスのマッピングを構築。"""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 2 and parts[0].isdigit():
                        self._uuid_to_index[parts[1]] = int(parts[0])
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def collect(self) -> list[GpuProcessInfo]:
        if not shutil.which("nvidia-smi"):
            return []

        if not self._uuid_to_index:
            self._build_uuid_map()

        try:
            result = subprocess.run(
                ["nvidia-smi",
                 "--query-compute-apps=pid,gpu_uuid,used_gpu_memory,process_name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

        if result.returncode != 0:
            return []

        processes: list[GpuProcessInfo] = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]

            try:
                pid = int(parts[0])
            except (ValueError, IndexError):
                continue

            gpu_uuid = parts[1] if len(parts) > 1 else ""
            gpu_index = self._uuid_to_index.get(gpu_uuid, 0)

            try:
                mem = float(parts[2]) if len(parts) > 2 else 0.0
            except ValueError:
                mem = 0.0

            raw_name = parts[3] if len(parts) > 3 else ""
            cmdline = _read_cmdline(pid)
            friendly = _friendly_name(cmdline, raw_name)

            processes.append(GpuProcessInfo(
                pid=pid,
                gpu_index=gpu_index,
                name=friendly,
                cmdline=cmdline,
                gpu_mem_mib=mem,
            ))

        # VRAM使用量でソート
        processes.sort(key=lambda p: p.gpu_mem_mib, reverse=True)
        return processes
