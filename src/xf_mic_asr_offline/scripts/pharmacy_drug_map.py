#!/usr/bin/env python3
# encoding: utf-8
"""药品名 → 颜色 的静态映射（纯逻辑，无 ROS 依赖，可离线单测）。

颜色键与 object_sortting.py 的 target_labels 保持一致：red / green / blue。
"""

# 默认映射：药品名 → 颜色键
DEFAULT_DRUG_COLOR_MAP = {
    "阿莫西林": "red",
    "布洛芬": "green",
    "维生素C": "blue",
}

# 合法颜色键（须与 object_sortting.target_labels 一致）
VALID_COLORS = ("red", "green", "blue")


def normalize(text):
    """归一化识别文本：None→空串，去除空格。"""
    if text is None:
        return ""
    return text.replace(" ", "").strip()


def lookup_drug_color(text, mapping=None):
    """从识别文本中提取药品名并返回 (drug, color)；未命中返回 None。

    采用子串包含匹配，兼容 "拿阿莫西林"、"我要布洛芬" 等带动词的指令；
    并做大小写无关匹配以兼容 "维生素C/维生素c"。
    """
    if mapping is None:
        mapping = DEFAULT_DRUG_COLOR_MAP
    norm = normalize(text)
    if not norm:
        return None
    for drug, color in mapping.items():
        drug_norm = normalize(drug)
        if drug_norm in norm or drug_norm.lower() in norm.lower():
            return (drug, color)
    return None
