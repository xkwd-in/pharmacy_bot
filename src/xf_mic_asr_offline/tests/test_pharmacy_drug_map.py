# encoding: utf-8
"""药品名→颜色映射的单元测试（纯逻辑，无需 ROS / 硬件）。"""
import os
import sys

# 把 scripts/ 加入 import 路径，避免依赖 catkin 安装
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from pharmacy_drug_map import (  # noqa: E402
    lookup_drug_color,
    normalize,
    DEFAULT_DRUG_COLOR_MAP,
    VALID_COLORS,
)


def test_lookup_returns_color_for_known_drug():
    assert lookup_drug_color("拿阿莫西林") == ("阿莫西林", "red")


def test_lookup_handles_verb_prefixes():
    assert lookup_drug_color("我要布洛芬") == ("布洛芬", "green")


def test_lookup_case_insensitive_vitamin():
    assert lookup_drug_color("帮我拿维生素c") == ("维生素C", "blue")


def test_lookup_strips_spaces():
    assert lookup_drug_color("拿 阿 莫 西 林") == ("阿莫西林", "red")


def test_lookup_unknown_returns_none():
    assert lookup_drug_color("开始分拣") is None


def test_lookup_empty_or_none_returns_none():
    assert lookup_drug_color("") is None
    assert lookup_drug_color(None) is None


def test_default_map_colors_are_valid():
    for color in DEFAULT_DRUG_COLOR_MAP.values():
        assert color in VALID_COLORS


def test_custom_mapping_overrides_default():
    custom = {"感冒灵": "red"}
    assert lookup_drug_color("拿感冒灵", custom) == ("感冒灵", "red")
    assert lookup_drug_color("拿阿莫西林", custom) is None


def test_normalize_removes_spaces_and_handles_none():
    assert normalize("拿 阿莫西林 ") == "拿阿莫西林"
    assert normalize(None) == ""
