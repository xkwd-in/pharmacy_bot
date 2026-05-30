"""
barcode_scanner.py — pyzbar 条码扫描模块

功能:
  - 扫描 cv2 图像中的条码/二维码
  - ROI 裁剪加速
  - 多帧稳定机制: 连续 3 帧同结果才返回
  - 支持 EAN-13, CODE-128, QR Code, UPC-A 等常见药盒条码

依赖:
  - pyzbar
  - opencv-python
  - numpy
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ---- pyzbar 解析缓存（修复 import 卡死）----
# 根因: 过去每帧最多 7 次在 _decode_image 内 `from pyzbar.pyzbar import decode`，
# Python 不缓存失败的 import，导致每帧重跑 ctypes.util.find_library('zbar') 的
# 子进程探测 (ldconfig/gcc/ld)；libzbar 不可用或解析慢时节点直接卡死。
# 解决: 全进程只解析一次，结果 (callable 或 None) 永久缓存；并用看门狗线程
# 对首次解析加超时，避免 find_library 阻塞时无限挂起。
_PYZBAR_INIT_TIMEOUT = 5.0  # 首次解析 pyzbar 的最长等待秒数
_pyzbar_lock = threading.Lock()
_pyzbar_resolved = False  # 是否已尝试过解析
_pyzbar_decode: Optional[Callable[[Any], Any]] = None  # 解析得到的 decode；None=不可用


def _load_pyzbar_decode() -> Optional[Callable[[Any], Any]]:
    """实际加载 pyzbar.decode（会触发 find_library/cdll）。
    失败返回 None，绝不抛异常。这是被看门狗线程调用、可被测试替换的接缝。
    """
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        return pyzbar_decode
    except Exception as exc:  # ImportError / OSError(找不到 libzbar) 等
        logger.error(
            "pyzbar 不可用，条码识别将降级跳过: %s "
            "(请在目标机安装 libzbar0 并执行 ldconfig)", exc,
        )
        return None


def _ensure_pyzbar(timeout: float = _PYZBAR_INIT_TIMEOUT) -> Optional[Callable[[Any], Any]]:
    """返回缓存的 pyzbar.decode（callable）或 None。

    全进程仅真正解析一次：成功缓存 callable，失败/超时缓存 None 且不再重试，
    从根本上杜绝每帧重复 import 触发 find_library 子进程探测导致的卡死。
    首次解析在守护线程中执行并施加超时，确保 find_library 阻塞时也能按时降级。
    """
    global _pyzbar_resolved, _pyzbar_decode
    if _pyzbar_resolved:
        return _pyzbar_decode

    with _pyzbar_lock:
        if _pyzbar_resolved:  # 双重检查，避免并发重复解析
            return _pyzbar_decode

        result: List[Optional[Callable[[Any], Any]]] = [None]

        def _worker() -> None:
            result[0] = _load_pyzbar_decode()

        t = threading.Thread(target=_worker, name="pyzbar-init", daemon=True)
        t.start()
        t.join(timeout)

        if t.is_alive():
            # find_library/ldconfig 等阻塞超时：标记不可用并缓存，后续不再重试。
            # 守护线程被遗弃（不阻塞进程退出），优于让节点永久卡死。
            logger.error(
                "pyzbar 解析超过 %.1fs 仍未完成（疑似 find_library/ldconfig 阻塞），"
                "条码识别已降级跳过。", timeout,
            )
            _pyzbar_decode = None
        else:
            _pyzbar_decode = result[0]

        _pyzbar_resolved = True
        return _pyzbar_decode


def _reset_pyzbar_cache() -> None:
    """重置 pyzbar 解析缓存（仅供测试使用）。"""
    global _pyzbar_resolved, _pyzbar_decode
    with _pyzbar_lock:
        _pyzbar_resolved = False
        _pyzbar_decode = None

# 支持的条码类型
SUPPORTED_SYMBOLOGIES = {"EAN13", "EAN-13", "CODE128", "CODE-128", "QRCODE",
                         "QR Code", "UPCA", "UPC-A", "CODE39", "CODE-39",
                         "ITF", "DATAMATRIX", "PDF417"}

# ---- 内部状态（多帧稳定） ----
_STATE: Dict[str, Any] = {
    "frame_buffer": [],  # list[list[dict]], 每帧的结果列表
    "stable_count": 0,
    "last_stable_result": None,
}


def _decode_image(cv2_image: np.ndarray) -> List[Dict[str, Any]]:
    """
    底层解码：调用 pyzbar 解码 cv2 图像。
    返回 list[dict]，每个 dict 包含 data, type, quality。
    不会抛出异常。
    """
    results: List[Dict[str, Any]] = []

    # pyzbar 解析只发生一次（缓存）；不可用时直接降级返回，绝不每帧重试 import。
    pyzbar_decode = _ensure_pyzbar()
    if pyzbar_decode is None:
        return results

    try:
        # pyzbar 支持传入 numpy 数组（cv2 BGR 格式需要转 RGB 吗？pyzbar 能处理 BGR）
        decoded_objects = pyzbar_decode(cv2_image)
    except Exception as exc:
        logger.warning("pyzbar decode error: %s", exc)
        return results

    for obj in decoded_objects:
        sym_type = obj.type  # e.g. 'EAN13', 'QRCODE'
        if sym_type not in SUPPORTED_SYMBOLOGIES:
            continue
        data_str = obj.data.decode("utf-8", errors="replace").strip()
        # quality: pyzbar 不直接提供质量分，用多边形点数和面积估算
        poly = obj.polygon
        if poly and len(poly) >= 4:
            area = cv2.contourArea(np.array([(p.x, p.y) for p in poly], dtype=np.int32))
            quality = min(1.0, area / 50000.0)  # 经验归一化
        else:
            quality = 0.5
        results.append({
            "data": data_str,
            "type": sym_type,
            "quality": round(quality, 4),
        })

    return results


def _preprocess_roi(cv2_image: np.ndarray) -> np.ndarray:
    """
    预处理 ROI：转换为灰度 → 高斯模糊 → 自适应阈值 → 形态学闭运算
    提升低质量条码识别率。
    """
    gray = cv2.cvtColor(cv2_image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    # 自适应阈值增强条码对比度
    thresh = cv2.adaptiveThreshold(blurred, 255,
                                   cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 11, 2)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    return closed


def _multi_region_decode(cv2_image: np.ndarray) -> List[Dict[str, Any]]:
    """
    多区域解码策略:
      1. 原图直接解码
      2. 原图 ROI 预处理后解码
      3. 若原图太大，裁剪中心 50% ROI 再解码
    结果去重合并。
    """
    from collections import OrderedDict

    all_results: List[Dict[str, Any]] = []
    seen_data: set = set()
    h, w = cv2_image.shape[:2]

    # 策略 1: 原图直接解码
    try:
        results = _decode_image(cv2_image)
        for r in results:
            if r["data"] not in seen_data:
                seen_data.add(r["data"])
                all_results.append(r)
    except Exception as exc:
        logger.debug("Direct decode failed: %s", exc)

    # 策略 2: 预处理后解码
    try:
        processed = _preprocess_roi(cv2_image)
        results = _decode_image(processed)
        for r in results:
            if r["data"] not in seen_data:
                seen_data.add(r["data"])
                all_results.append(r)
    except Exception as exc:
        logger.debug("Processed decode failed: %s", exc)

    # 策略 3: 中心 ROI (如果图像较大)
    if w > 300 and h > 300:
        try:
            cx, cy = w // 2, h // 2
            roi_w, roi_h = w // 2, h // 2
            x1 = max(0, cx - roi_w // 2)
            y1 = max(0, cy - roi_h // 2)
            roi = cv2_image[y1:y1 + roi_h, x1:x1 + roi_w]
            if roi.size > 0:
                results = _decode_image(roi)
                for r in results:
                    if r["data"] not in seen_data:
                        seen_data.add(r["data"])
                        all_results.append(r)
        except Exception as exc:
            logger.debug("ROI decode failed: %s", exc)

    # 策略 4: 边缘 ROI — 左、右、上、下 1/3 条带（药盒可能只拍到一角）
    if w > 300 and h > 300:
        third_w, third_h = w // 3, h // 3
        regions = [
            ("left", cv2_image[0:h, 0:third_w]),
            ("right", cv2_image[0:h, w - third_w:w]),
            ("top", cv2_image[0:third_h, 0:w]),
            ("bottom", cv2_image[h - third_h:h, 0:w]),
        ]
        for _name, region in regions:
            if region.size == 0:
                continue
            try:
                results = _decode_image(region)
                for r in results:
                    if r["data"] not in seen_data:
                        seen_data.add(r["data"])
                        all_results.append(r)
            except Exception:
                pass

    return all_results


def reset_stability() -> None:
    """重置多帧稳定状态（切换场景时调用）"""
    _STATE["frame_buffer"] = []
    _STATE["stable_count"] = 0
    _STATE["last_stable_result"] = None


def scan_barcode(cv2_image: np.ndarray,
                 stability_frames: int = 3,
                 reset: bool = False) -> List[Dict[str, Any]]:
    """
    扫描图像中的条码/二维码，带多帧稳定机制。

    Args:
        cv2_image: OpenCV BGR numpy 数组图像。
        stability_frames: 连续多少帧相同结果才返回（默认 3）。
        reset: 是否重置帧缓存（用于场景切换）。

    Returns:
        list[dict]: 每个 dict 包含 {data, type, quality}。
                    空列表表示当前帧无稳定条码。
    """
    if cv2_image is None or cv2_image.size == 0:
        logger.warning("scan_barcode: received empty image")
        return []

    if reset:
        reset_stability()

    if stability_frames < 1:
        stability_frames = 1

    try:
        current_results = _multi_region_decode(cv2_image)
    except Exception as exc:
        logger.error("scan_barcode: multi-region decode error: %s", exc)
        return []

    # 当前帧无结果 → 清空缓存
    if not current_results:
        _STATE["frame_buffer"] = []
        _STATE["stable_count"] = 0
        return []

    # 提取当前帧的 data 集合（用于比较）
    current_data_set = frozenset(r["data"] for r in current_results)

    # 与上一帧比较
    if _STATE["last_stable_result"] is not None:
        prev_data_set = frozenset(r["data"] for r in _STATE["last_stable_result"])
        if current_data_set == prev_data_set:
            _STATE["stable_count"] += 1
        else:
            _STATE["stable_count"] = 0

    _STATE["last_stable_result"] = current_results

    # 达到稳定帧数要求 → 返回结果
    if _STATE["stable_count"] >= stability_frames - 1:
        # 按 quality 降序排列
        sorted_results = sorted(current_results, key=lambda x: x["quality"], reverse=True)
        return sorted_results

    # 未稳定 → 返回空
    return []


# ============ 独立测试 ============
def _generate_test_barcode(data: str, sym_type: str = "EAN13") -> np.ndarray:
    """
    生成模拟条码图像用于测试（无实际编码，仅生成带条纹的测试图案）。
    实际运行时不会调用此函数；仅用于 main 自测。
    """
    import random
    random.seed(42)
    h, w = 200, 400
    img = np.ones((h, w, 3), dtype=np.uint8) * 255
    # 画一些随机黑白条纹模拟条码
    for i, ch in enumerate(data[:20]):
        x = i * 18 + 20
        color = 0 if (ord(ch) % 2 == 0) else 255
        cv2.rectangle(img, (x, 30), (x + 10, h - 30),
                      (color, color, color), -1)
    cv2.putText(img, f"TEST:{data[:12]}", (20, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
    return img


def main() -> None:
    """独立运行测试"""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("=== barcode_scanner self-test ===")

    # 测试空输入
    logger.info("Test 1: empty image -> %s", scan_barcode(np.zeros((0, 0, 3), dtype=np.uint8)))
    logger.info("Test 2: None input -> %s", scan_barcode(None))  # type: ignore

    # 测试模拟图像
    test_img = _generate_test_barcode("123456789012", "EAN13")
    cv2.imwrite("/tmp/test_barcode.png", test_img)
    logger.info("Test 3: simulated barcode image saved to /tmp/test_barcode.png")

    # 测试多帧稳定
    reset_stability()
    for i in range(5):
        result = scan_barcode(test_img, stability_frames=3)
        logger.info("Frame %d -> %s", i + 1, result if result else "(no stable result yet)")

    # 测试重置
    reset_stability()
    logger.info("After reset: %s", scan_barcode(test_img, stability_frames=3))

    logger.info("=== self-test done ===")


if __name__ == "__main__":
    main()
