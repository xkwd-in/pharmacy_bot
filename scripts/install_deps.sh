#!/usr/bin/env bash
# ============================================================
# install_deps.sh — PharmacyBot 一键安装依赖
# 用法: bash scripts/install_deps.sh
# 注意: 请确保在 Jetson / 目标机器上运行（非 Hermes venv 环境）
# ============================================================
set -e

echo "===== PharmacyBot 系统依赖安装 ====="

# ── 系统级依赖 ──────────────────────────────────────────────
echo "[1/3] 安装系统依赖 (libzbar0 — pyzbar 需要)..."
sudo apt update -qq
sudo apt install libzbar0 -y
# 刷新动态链接库缓存：确保 pyzbar 的 ctypes.util.find_library('zbar')
# 命中 ldconfig 快速路径，避免回退到 gcc/ld 子进程探测（冷缓存时可能阻塞/卡死）。
sudo ldconfig
echo "✔ 系统依赖安装完成"

# ── Python 基础视觉库 ──────────────────────────────────────
echo "[2/3] 安装 Python 视觉库 (pyzbar + opencv-python-headless)..."
pip install pyzbar opencv-python-headless --quiet
echo "✔ 视觉库安装完成"

# ── PaddleOCR + PaddlePaddle ───────────────────────────────
echo "[3/3] 安装 PaddleOCR + PaddlePaddle (轻量版)..."
pip install paddleocr paddlepaddle --quiet
echo "✔ PaddleOCR 安装完成"

# ── 完成 ────────────────────────────────────────────────────
echo ""
echo "===== ✅ 所有依赖安装完成 ====="
echo "可通过以下命令验证:"
echo "  python -c \"import pyzbar; print('pyzbar OK')\""
echo "  python -c \"import cv2; print('OpenCV', cv2.__version__)\""
echo "  python -c \"from paddleocr import PaddleOCR; print('PaddleOCR OK')\""
