"""外观色板单一事实源测试（2026-09-06）。

断言：10 套方案齐全；深浅中性两套；派生数学（低饱和中性规则/深浅 hover 方向/
accent_on 亮度判定）；resolve 全路径不抛错。
"""

import re

from agent.theme_palettes import (CUSTOM_ID, DEFAULT_CUSTOM, LOW_SAT_THRESHOLD,
                                  NEUTRAL, SCHEMES, SCHEME_ORDER, _hex_rgb,
                                  _luminance, _saturation, derive_accent,
                                  resolve_palette)


def test_schemes_ten_and_order_stable():
    assert len(SCHEMES) == 10
    assert len(SCHEME_ORDER) == 10
    assert set(SCHEME_ORDER) == set(SCHEMES.keys())
    # 品牌默认与岩石灰必在
    assert "rose_mist" in SCHEMES and "graphite" in SCHEMES


def test_neutral_two_modes_full_keys():
    for mode in ("dark", "light"):
        n = NEUTRAL[mode]
        for key in ("window_bg", "sidebar_bg", "card_bg", "hover_bg", "border",
                    "text_primary", "text_secondary", "text_muted", "success",
                    "warning", "danger"):
            assert key in n, f"{mode} 缺 {key}"
    assert NEUTRAL["dark"]["window_bg"] != NEUTRAL["light"]["window_bg"]


def test_derive_accent_low_saturation_is_neutral():
    """岩石灰/近灰种子：标记 neutral，且不产出过饱和杂色。"""
    a = derive_accent("#737373", is_dark=True)
    assert a["neutral"] is True
    assert _saturation(a["accent"]) <= LOW_SAT_THRESHOLD + 0.01
    assert _saturation(DEFAULT_CUSTOM) <= LOW_SAT_THRESHOLD


def test_derive_accent_hover_direction_by_mode():
    """深色模式 hover 向白混（变亮），浅色模式向黑混（变深）。"""
    seed = "#4A6CF7"
    dark_h = derive_accent(seed, is_dark=True)["accent_hover"]
    light_h = derive_accent(seed, is_dark=False)["accent_hover"]
    assert _luminance(dark_h) > _luminance(seed), "深色 hover 应变亮"
    assert _luminance(light_h) < _luminance(seed), "浅色 hover 应变深"


def test_derive_accent_on_contrast_rule():
    """亮 seed 用深字，暗 seed 用白字（accent_on）。"""
    assert derive_accent("#E0A422", is_dark=True)["accent_on"] == "#1b1b1f"
    assert derive_accent("#4A6CF7", is_dark=True)["accent_on"] == "#ffffff"


def test_resolve_palette_all_schemes_both_modes():
    for mode in ("dark", "light"):
        for sid in SCHEME_ORDER:
            pal = resolve_palette(mode, sid)
            assert pal["theme_mode"] == mode
            assert pal["accent"], f"{mode}/{sid} 缺 accent"
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", pal["accent"])
    # auto + 系统判定
    assert resolve_palette("auto", None, None, is_system_dark=True)["theme_mode"] == "dark"
    assert resolve_palette("auto", None, None, is_system_dark=False)["theme_mode"] == "light"
    # custom 路径
    pal = resolve_palette("dark", CUSTOM_ID, "#F59E0B")
    assert pal["accent"] == "#F59E0B"


def test_hex_rgb_3digit_expansion():
    assert _hex_rgb("#abc") == (0xAA, 0xBB, 0xCC)
    assert _hex_rgb("#0a0a1a") == (0x0A, 0x0A, 0x1A)
