"""
回归测试: pyzbar import 卡死问题 (per-frame 重复 import + find_library 阻塞)

根因:
  _decode_image 过去在函数体内 `from pyzbar.pyzbar import decode`，
  而该函数每帧被调用最多 7 次 (multi-region)。Python 不缓存失败的 import，
  因此当 libzbar 不能被立即解析时 (缺 libzbar0 / ldconfig 冷缓存 / NFS / Jetson
  慢工具链)，每帧都会重新触发 ctypes.util.find_library('zbar') 的子进程探测
  (ldconfig/gcc/ld)，节点表现为"卡死"。

本测试钉死正确行为:
  1. pyzbar 解析在整个进程内最多发生一次 (缓存)，不再每帧重试。
  2. pyzbar 不可用时优雅降级 (_decode_image 返回 [])，不抛异常。
  3. 解析逻辑阻塞时不会无限挂起 (有超时看门狗)。
"""

import time

import numpy as np
import pytest

from ocr_barcode import barcode_scanner as bs


@pytest.fixture(autouse=True)
def _reset_cache():
    bs._reset_pyzbar_cache()
    yield
    bs._reset_pyzbar_cache()


def _img():
    return np.zeros((50, 50, 3), dtype=np.uint8)


def test_pyzbar_resolved_at_most_once_across_many_frames(monkeypatch):
    """核心回归: 多帧多区域解码只解析 pyzbar 一次。"""
    calls = {"n": 0}

    def fake_loader():
        calls["n"] += 1
        return None  # 模拟不可用

    monkeypatch.setattr(bs, "_load_pyzbar_decode", fake_loader)
    bs._reset_pyzbar_cache()

    img = _img()
    for _ in range(20):  # 20 帧 × 数次/帧
        bs._decode_image(img)

    assert calls["n"] == 1, f"pyzbar 被解析 {calls['n']} 次 (期望 1 次，否则即为卡死根因)"


def test_decode_returns_empty_when_pyzbar_unavailable(monkeypatch):
    """pyzbar 不可用时优雅降级，返回空列表而非抛异常。"""
    monkeypatch.setattr(bs, "_load_pyzbar_decode", lambda: None)
    bs._reset_pyzbar_cache()
    assert bs._decode_image(_img()) == []


def test_ensure_pyzbar_returns_and_caches_callable(monkeypatch):
    """成功路径: 返回 decode 可调用对象并缓存，不重复解析。"""
    calls = {"n": 0}
    sentinel = lambda image: []  # noqa: E731

    def loader():
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(bs, "_load_pyzbar_decode", loader)
    bs._reset_pyzbar_cache()

    assert bs._ensure_pyzbar() is sentinel
    assert bs._ensure_pyzbar() is sentinel
    assert calls["n"] == 1


def test_ensure_pyzbar_does_not_hang_when_loader_blocks(monkeypatch):
    """看门狗: 解析阻塞 (模拟 find_library 挂死) 时按超时降级，不无限挂起。"""

    def blocking_loader():
        time.sleep(30)  # 模拟 find_library/ldconfig 挂死

    monkeypatch.setattr(bs, "_load_pyzbar_decode", blocking_loader)
    bs._reset_pyzbar_cache()

    start = time.time()
    decode = bs._ensure_pyzbar(timeout=1.0)
    elapsed = time.time() - start

    assert decode is None, "阻塞时应降级为不可用"
    assert elapsed < 5.0, f"_ensure_pyzbar 挂起了 {elapsed:.1f}s (期望按 1s 超时返回)"


def test_blocking_loader_does_not_retry_after_timeout(monkeypatch):
    """超时后标记为不可用并缓存，后续帧不再重试 (避免反复挂起)。"""
    calls = {"n": 0}

    def blocking_loader():
        calls["n"] += 1
        time.sleep(30)

    monkeypatch.setattr(bs, "_load_pyzbar_decode", blocking_loader)
    bs._reset_pyzbar_cache()

    for _ in range(5):
        assert bs._ensure_pyzbar(timeout=0.5) is None
    assert calls["n"] == 1, f"超时后仍重试了 {calls['n']} 次 (期望仅 1 次)"
