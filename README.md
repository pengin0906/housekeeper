# housekeeper

システムモニター + GPU/PCIe/NFS/温度 完全対応**

バーメーター形式で、CPU・メモリ・ディスク・ネットワーク・GPU・PCIe・NFS/SAN/NAS・温度を一画面で監視できるツール。

## 特徴

- **GPU 完全対応**: NVIDIA (nvidia-smi/pynvml), AMD ROCm (rocm-smi), Intel Gaudi (hl-smi)
- **PCIe デバイス一覧**: Gen1〜Gen6 対応、リンク速度・幅・帯域表示 + デバイスごとの実 I/O スループット
- **温度監視**: CPU, NVMe SSD, GPU 等の温度を hwmon + GPU ドライバから一括表示
- **NFS/SAN/NAS 監視**: NFS, CIFS/SMB, GlusterFS, Ceph, Lustre 等のネットワークストレージ
- **ネットワーク分類**: WAN/LAN/仮想インターフェースを自動分類
- **プロセス監視**: CPU/メモリ使用率上位プロセス + GPU を使用しているプロセス
- **カーネル情報**: Load Average, Uptime, Context Switches, Interrupts
- **3つの動作モード**: TUI (curses), テキスト出力, X11 GUI (tkinter)
- **軽量設計**: GPU がない環境では GPU モジュールを一切ロードしない遅延ロード
- **外部依存ゼロ**: Python 標準ライブラリのみで動作

## インストール

```bash
# リポジトリをクローン
git clone <url>
cd housekeeper

# editable install
pip install -e .

# または直接実行
python -m housekeeper.main
```

## 使い方

```bash
# TUI モード (デフォルト) - ターミナル内で動作
housekeeper

# X11 GUI ウィンドウモード
housekeeper -x
housekeeper --gui

# テキスト出力 (スクリプト連携やリダイレクト向け)
housekeeper --text

# オプション
housekeeper -i 0.5              # 0.5秒間隔で更新
housekeeper --no-per-core       # CPU をコアごとに表示しない (合計のみ)
housekeeper --no-gpu            # GPU モニタリングを無効化
housekeeper --detect            # 利用可能なハードウェアを検出して表示
```

### キー操作 (TUI / GUI モード)

| キー | 動作 |
|------|------|
| `q` / `ESC` | 終了 |
| `c` | CPU per-core 表示の切り替え |
| `p` | PCIe デバイス表示の切り替え |
| `+` | 更新間隔を短く (0.5秒刻み) |
| `-` | 更新間隔を長く (0.5秒刻み) |

## アーキテクチャ解説

### プロジェクト構成

```
housekeeper/
├── pyproject.toml              # パッケージ設定
├── README.md
└── housekeeper/
    ├── __init__.py
    ├── main.py                 # エントリポイント、遅延ロード制御
    ├── collectors/             # データ収集モジュール
    │   ├── cpu.py              # /proc/stat → CPU 使用率
    │   ├── memory.py           # /proc/meminfo → メモリ/スワップ
    │   ├── disk.py             # /proc/diskstats → ディスク I/O
    │   ├── network.py          # /proc/net/dev → ネットワーク I/O (WAN/LAN分類)
    │   ├── kernel.py           # /proc/loadavg, /proc/uptime → カーネル統計
    │   ├── process.py          # /proc/[pid]/stat → トッププロセス
    │   ├── pcie.py             # /sys/bus/pci/devices → PCIe リンク情報 + I/O
    │   ├── nfs.py              # /proc/mounts, mountstats → NFS/SAN/NAS
    │   ├── temperature.py      # /sys/class/hwmon → 温度センサー
    │   ├── gpu.py              # nvidia-smi / pynvml → NVIDIA GPU
    │   ├── gpu_process.py      # nvidia-smi → GPU プロセス情報
    │   ├── amd_gpu.py          # rocm-smi → AMD GPU (MI300 等)
    │   └── gaudi.py            # hl-smi → Intel Gaudi
    └── ui/
        ├── colors.py           # curses 色ペア定義
        ├── bar.py              # バーメーター描画 (Unicode ブロック文字)
        ├── renderer.py         # curses TUI レンダラー
        ├── text_renderer.py    # ANSI テキスト出力
        └── gui.py              # tkinter X11 GUI
```

### コレクター設計

各コレクターは以下の原則に基づいている:

1. **`/proc` ファイルシステムの直接読み取り**: CPU, メモリ, ディスク, ネットワークは外部コマンドを一切使わず `/proc` から直接読む。最も高速で権限問題なし
2. **差分計算**: CPU, ディスク, ネットワーク等の累積値は、2回のサンプリング差分から/秒を算出
3. **遅延ロード**: `main.py` が `shutil.which()` でコマンドの存在を確認し、必要なコレクターだけを `importlib.import_module()` で動的にロード

```
起動フロー:
  nvidia-smi あり? → gpu.py, gpu_process.py をロード
  rocm-smi あり?   → amd_gpu.py をロード
  hl-smi あり?     → gaudi.py をロード
  NFS マウントあり? → nfs.py をロード
  /sys/bus/pci あり? → pcie.py をロード
  /sys/class/hwmon あり? → temperature.py をロード
```

### ネットワーク分類ロジック

```
/proc/net/route の default route → WAN (インターネット向け)
docker*, veth*, br-* 等          → VIRTUAL (仮想)
それ以外                          → LAN (ローカル)
```

### PCIe 帯域計算

```
PCIe Gen5 x16 = 3.938 GB/s × 16 lanes = 63.0 GB/s (双方向)
PCIe Gen4 x4  = 1.969 GB/s × 4 lanes  =  7.9 GB/s
```

## 対応ハードウェア

| カテゴリ | 対応 |
|---------|------|
| CPU | 全 x86/ARM Linux (per-core 対応) |
| メモリ | DDR4/DDR5/HBM |
| ディスク | NVMe, SATA SSD/HDD, virtio |
| ネットワーク | 1GbE, 10GbE, 25GbE, 100GbE, InfiniBand |
| NVIDIA GPU | GeForce, RTX, Quadro, Tesla, A100, H100, B100, Blackwell |
| AMD GPU | MI300X/A, MI250X, MI210, RX 7000 (ROCm) |
| Intel Gaudi | Gaudi, Gaudi2, Gaudi3 |
| NFS/SAN/NAS | NFS v3/v4, CIFS/SMB, iSCSI, GlusterFS, Ceph, Lustre |
| PCIe | Gen1〜Gen6 |
| 温度 | CPU (k10temp/coretemp), NVMe, GPU, マザーボード |

## ライセンス

MIT License
