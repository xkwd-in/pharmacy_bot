"""
三模态视觉融合识别管线
==============================
主路：pyzbar       条码 / 二维码 → 99% 单条命中，<10ms
辅路：PaddleOCR    PP-OCRv4 → 文字 / 批号 / 有效期
兜底：YOLOv8s      自标药盒 / 药瓶 / 分装袋

输出：drug_id（数据库主键）+ 三路投票后的融合置信度
"""

import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 路径置信度权重（基于各路单测准确率）
W_BARCODE = 0.50   # 主路权重最大
W_OCR     = 0.30
W_YOLO    = 0.20


@dataclass
class RecognitionResult:
    drug_id: Optional[int]
    drug_name: Optional[str]
    confidence: float
    detected_by: List[str]      # 哪几路命中
    bbox: Optional[Tuple[int, int, int, int]]    # x, y, w, h


class VisionPipeline:
    """三模态视觉融合识别"""

    def __init__(self, db_session,
                 yolo_weights: str = "models/yolov8s_drugs.pt",
                 ocr_lang: str = "ch"):
        self.db = db_session

        # 1. 条码（pyzbar）
        try:
            from pyzbar.pyzbar import decode as zbar_decode
            self.zbar_decode = zbar_decode
        except ImportError:
            logger.warning("pyzbar 未安装")
            self.zbar_decode = None

        # 2. PaddleOCR
        try:
            from paddleocr import PaddleOCR
            self.ocr = PaddleOCR(use_angle_cls=True, lang=ocr_lang, show_log=False)
        except ImportError:
            logger.warning("PaddleOCR 未安装")
            self.ocr = None

        # 3. YOLOv8
        try:
            from ultralytics import YOLO
            self.yolo = YOLO(yolo_weights) if yolo_weights else None
        except ImportError:
            logger.warning("ultralytics 未安装")
            self.yolo = None

    # === 主入口 ===
    def recognize(self, image: np.ndarray) -> RecognitionResult:
        """三路并行识别 → 投票融合"""
        results_per_path = {}

        if self.zbar_decode is not None:
            results_per_path["barcode"] = self._run_barcode(image)
        if self.ocr is not None:
            results_per_path["ocr"] = self._run_ocr(image)
        if self.yolo is not None:
            results_per_path["yolo"] = self._run_yolo(image)

        return self._fuse(results_per_path)

    # === 单路实现 ===
    def _run_barcode(self, image) -> Optional[dict]:
        """pyzbar 主路：扫码"""
        decoded = self.zbar_decode(image)
        if not decoded:
            return None
        code_str = decoded[0].data.decode("utf-8")
        rect = decoded[0].rect
        drug = self._lookup_by_barcode(code_str)
        if drug is None:
            return None
        return {
            "drug_id": drug["id"],
            "drug_name": drug["name"],
            "confidence": 0.99,
            "bbox": (rect.left, rect.top, rect.width, rect.height),
        }

    def _run_ocr(self, image) -> Optional[dict]:
        """PaddleOCR 辅路：识别药名文字"""
        result = self.ocr.ocr(image, cls=True)
        if not result or not result[0]:
            return None

        # 拼接所有识别文字
        texts = [line[1][0] for line in result[0]]
        confidence_avg = float(np.mean([line[1][1] for line in result[0]]))
        joined_text = " ".join(texts)

        # 在药品名 fuzzy 匹配
        drug = self._fuzzy_lookup_by_name(joined_text)
        if drug is None:
            return None

        # 取第一行 bbox
        bbox_pts = result[0][0][0]
        xs = [p[0] for p in bbox_pts]
        ys = [p[1] for p in bbox_pts]
        x, y = int(min(xs)), int(min(ys))
        w, h = int(max(xs) - x), int(max(ys) - y)

        return {
            "drug_id": drug["id"],
            "drug_name": drug["name"],
            "confidence": confidence_avg,
            "bbox": (x, y, w, h),
        }

    def _run_yolo(self, image) -> Optional[dict]:
        """YOLOv8 兜底：药盒形态识别"""
        results = self.yolo.predict(image, verbose=False, conf=0.4)
        if not results or len(results[0].boxes) == 0:
            return None

        box = results[0].boxes[0]
        cls_id = int(box.cls.item())
        conf = float(box.conf.item())
        drug = self._lookup_by_yolo_class(cls_id)
        if drug is None:
            return None

        xyxy = box.xyxy.cpu().numpy()[0]
        x, y, x2, y2 = map(int, xyxy)
        return {
            "drug_id": drug["id"],
            "drug_name": drug["name"],
            "confidence": conf,
            "bbox": (x, y, x2 - x, y2 - y),
        }

    # === 投票融合 ===
    def _fuse(self, results_per_path: dict) -> RecognitionResult:
        """投票 + 置信度加权融合"""
        # 收集每个 drug_id 的加权得分
        score_per_drug = {}
        bboxes = {}
        detected_by_per_drug = {}

        weights = {"barcode": W_BARCODE, "ocr": W_OCR, "yolo": W_YOLO}

        for path, r in results_per_path.items():
            if r is None:
                continue
            did = r["drug_id"]
            w = weights[path]
            score_per_drug[did] = score_per_drug.get(did, 0.0) + w * r["confidence"]
            bboxes.setdefault(did, r["bbox"])
            detected_by_per_drug.setdefault(did, []).append(path)

        if not score_per_drug:
            return RecognitionResult(None, None, 0.0, [], None)

        best_did = max(score_per_drug, key=score_per_drug.get)
        best_score = score_per_drug[best_did]
        drug = self._lookup_by_id(best_did)
        return RecognitionResult(
            drug_id=best_did,
            drug_name=drug["name"] if drug else None,
            confidence=best_score,
            detected_by=detected_by_per_drug[best_did],
            bbox=bboxes.get(best_did),
        )

    # === 数据库查询占位 ===
    def _lookup_by_barcode(self, code: str) -> Optional[dict]:
        """从数据库按条码反查药品。TODO: 真实接 sqlalchemy"""
        # cur = self.db.execute("SELECT id,name FROM drugs WHERE barcode=?", (code,))
        # row = cur.fetchone()
        # return {"id": row[0], "name": row[1]} if row else None
        return {"id": 1, "name": f"演示药品[{code}]"}    # stub

    def _fuzzy_lookup_by_name(self, text: str) -> Optional[dict]:
        # TODO: 用 rapidfuzz / Trie 做模糊匹配
        return {"id": 2, "name": "OCR命中演示药品"}

    def _lookup_by_yolo_class(self, cls_id: int) -> Optional[dict]:
        # TODO: cls_id → drug_id 映射表
        return {"id": 3, "name": f"YOLO类别{cls_id}演示药品"}

    def _lookup_by_id(self, drug_id: int) -> Optional[dict]:
        return {"id": drug_id, "name": f"药品{drug_id}"}    # stub


# === 可视化（debug + demo 用）===
def draw_results(image: np.ndarray, result: RecognitionResult) -> np.ndarray:
    """在图像上画 bbox + 文字标注，用于 demo 视频中展示三路融合过程"""
    if result.bbox is None:
        return image
    x, y, w, h = result.bbox
    color_map = {"barcode": (0, 255, 0), "ocr": (0, 255, 255), "yolo": (0, 0, 255)}
    # 多路命中画多色叠加框
    for i, path in enumerate(result.detected_by):
        cv2.rectangle(image, (x - i * 2, y - i * 2), (x + w + i * 2, y + h + i * 2),
                      color_map[path], 2)
    label = f"{result.drug_name} | conf={result.confidence:.2f} | {'+'.join(result.detected_by)}"
    cv2.putText(image, label, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return image


if __name__ == "__main__":
    # 自测：读一张测试图
    import sys
    img_path = sys.argv[1] if len(sys.argv) > 1 else "test.jpg"
    img = cv2.imread(img_path)
    if img is None:
        print(f"无法读取 {img_path}")
        sys.exit(1)

    pipeline = VisionPipeline(db_session=None, yolo_weights=None)
    res = pipeline.recognize(img)
    print(f"识别结果: drug_id={res.drug_id} name={res.drug_name} "
          f"conf={res.confidence:.3f} by={res.detected_by}")
