"""网文毒点探针回归测试。

检测巧合堆叠、情绪夸张、打脸循环、优越感炫耀等网文常见套路。
"""

from __future__ import annotations

from novel_ledger_core.content.style_check import check_cliche_saturation, check_style_hard


def test_coincidence_saturation():
    """巧合堆叠：正巧/恰好/刚好等高频出现。"""
    text = (
        "他正巧路过这里，恰好看见了那个人。"
        "刚好今天有空，偏偏又碰巧遇到了老友。"
        "不巧的是，这次又正巧撞见了。"
    )
    result = check_cliche_saturation(text)
    assert result["ok"] is False
    assert any(f["metric"] == "巧合堆叠" for f in result["fails"])
    detail = result["details"]["巧合堆叠"]
    assert detail["count"] >= 5


def test_cliche_density_is_advisory_in_chapter_gate():
    text = "他正巧赶上，恰好看见掌柜，偏偏这时又有人来。"
    result = check_style_hard(text)
    assert result["ok"] is True
    assert any(item["metric"] == "巧合堆叠" for item in result["warnings"])


def test_emotion_exaggeration():
    """情绪夸张：震惊/骇然/惊呆等过度情绪词。"""
    text = (
        "他震惊地看着眼前的一切。"
        "众人骇然失色。"
        "她惊呆了，目瞪口呆地站在原地。"
        "所有人都大吃一惊，简直瞠目结舌。"
    )
    result = check_cliche_saturation(text)
    assert result["ok"] is False
    assert any(f["metric"] == "情绪夸张" for f in result["fails"])


def test_face_slapping_loop():
    """打脸循环：你不是说XX吗？的重复模式。"""
    text = (
        "你不是说不来了吗？怎么又来了？"
        "你不是说这事办不成吗？现在又怎么说？"
        "你不是说他已经走了吗？怎么还在这？"
        "你不是说明天才来吗？"
    )
    result = check_cliche_saturation(text)
    assert result["ok"] is False
    assert any(f["metric"] == "打脸循环" for f in result["fails"])
    # 应该检测到至少3次（>阈值2）
    detail = result["details"]["打脸循环"]
    assert detail["count"] > 2


def test_superiority_display():
    """优越感炫耀：殊不知/哪里知道等信息不对等炫耀。"""
    text = (
        "他们哪里知道，其实这一切早在主角的计划之中。"
        "殊不知，主角早已看穿了一切，实际上他只是在演戏。"
        "他们不知道的是，其实灵石早就被掉包了。"
    )
    result = check_cliche_saturation(text)
    assert result["ok"] is False
    assert any(f["metric"] == "优越感炫耀" for f in result["fails"])
    # 应该检测到至少2次（>阈值1）
    detail = result["details"]["优越感炫耀"]
    assert detail["count"] > 1


def test_occasional_usage_passes():
    """偶发使用不触发：每类1-2次应该通过。"""
    text = (
        "他正巧路过柜台，看见掌柜在算账。"
        "这事让人震惊。"
        "你不是说明天来吗？"
        "他不知道的是，灵石已经不见了。"
        "他扶着墙挪到门边，推开门看了看。"
        "外头下着小雨，石板路上积了浅浅的水。"
    )
    result = check_cliche_saturation(text)
    # 每类最多1次，应该全部通过
    assert result["ok"] is True, result["fails"]


def test_clean_text_passes():
    """无网文毒点的正常文本应该通过。"""
    text = (
        "他把碗推到桌角，抹了把手，从灶膛里夹出半截没烧透的柴。"
        "掌柜不说话，只是盯着那张凭据。"
        "外头有人喊了一声，他没应。"
        "灯油快见底了，得省着点用。"
    )
    result = check_cliche_saturation(text)
    assert result["ok"] is True
    assert len(result["fails"]) == 0
