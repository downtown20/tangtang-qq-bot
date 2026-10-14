"""外观主题色板单一事实源（2026-09-06 外观设置改版）。

模型：黑白灰中性基础层（浅/深两套，不随配色）+ 10 套配色方案（seed 派生 accent 系）。
借鉴 MotrixNext（种子色→派生）与 CC Switch（明度微差层级）——
中性层不染色，配色只染 accent；低饱和种子自动走中性（content 规则）。

用法：resolve_palette(theme_mode, scheme_id, custom_hex) -> dict（QSS 生成与 UI 共用）。
"""
from __future__ import annotations

# ═══════════════════════════════════════════
# 中性基础层（黑白灰——CC Switch 明度微差公式）
# ═══════════════════════════════════════════
NEUTRAL = {
    "dark": {
        "window_bg":   "#17171a",   # 窗口底（最底）
        "sidebar_bg":  "#101013",   # 侧栏（最深）
        "card_bg":     "#1f1f23",   # 卡片（高一档）
        "hover_bg":    "#27272b",   # 悬浮底
        "border":      "#333338",   # 弱描边
        "border_hi":   "#3f3f46",   # 强描边（hover 边框）
        "text_primary":   "#ececef",
        "text_secondary": "#a1a1aa",
        "text_muted":     "#6b6b74",
        "input_bg":    "#141417",   # 输入/内嵌深底
        "log_bg":      "#101013",
        "log_text":    "#d6d6db",
        "highlight":   "rgba(255, 255, 255, 0.07)",  # 1px 高光
        # 语义状态色（不随配色；暗色低饱和可辨识）
        "success": "#6fbf9e", "warning": "#e0a868",
        "danger": "#e07a8e", "danger_bg": "#3a2229",
    },
    "light": {
        # Aether 设计系统（2026-09-06，UI参考/bad-dodo-83/css/system.css 令牌映射）：
        # pearl 底 + 玻璃白面 + 炭黑墨色三阶 + hairline 描边。Qt 无 backdrop-blur，
        # 玻璃近似为 pearl 上的近白实色（DESIGN.md 无障碍条款：hairline 是唯一幸存边）。
        "window_bg":   "#eef1f6",   # pearl（页面底）
        "sidebar_bg":  "#e6ebf3",   # glass 55% over pearl
        "card_bg":     "#ffffff",   # glass-strong 75% ≈ 近白
        "hover_bg":    "#e9edf3",   # rgba(15,23,42,0.04) over pearl
        "border":      "rgba(15, 23, 42, 0.08)",   # hairline
        "border_hi":   "rgba(15, 23, 42, 0.14)",   # hairline-strong
        "text_primary":   "#1d1d1f",   # ink（炭黑锚点）
        "text_secondary": "#54545a",   # slate
        "text_muted":     "#86868b",   # mist
        "input_bg":    "#ffffff",      # glass-strong 输入面
        "log_bg":      "#f4f6fa",
        "log_text":    "#54545a",
        "highlight":   "rgba(255, 255, 255, 0.9)",  # bevel-light（卡顶高光）
        "success": "#34c759", "warning": "#ff9f0a",
        "danger": "#ff453a", "danger_bg": "#ffe9e7",
    },
}

# ═══════════════════════════════════════════
# 10 套配色方案（seed 派生 accent；MotrixNext 色系 + 品牌雾玫瑰）
# ═══════════════════════════════════════════
SCHEMES = {
    "rose_mist": {"name": "雾玫瑰", "seed": "#e2a1bc"},   # 品牌：优雅（默认候选之一）
    "amber":     {"name": "琥珀金", "seed": "#E0A422"},   # 暖金高级
    "space":     {"name": "深空蓝", "seed": "#4A6CF7"},   # 沉稳
    "mint":      {"name": "薄荷翠", "seed": "#10B981"},   # 清新
    "aurora":    {"name": "极光紫", "seed": "#8B5CF6"},   # 神秘
    "coral":     {"name": "珊瑚橙", "seed": "#F97316"},   # 活力
    "glacier":   {"name": "冰川蓝", "seed": "#06B6D4"},   # 清爽
    "evergreen": {"name": "森林绿", "seed": "#15803D"},   # 自然
    "graphite":  {"name": "岩石灰", "seed": "#737373"},   # 黑白灰纯中性（开箱默认）
    "rose":      {"name": "玫红",   "seed": "#F43F5E"},   # 鲜明
}
SCHEME_ORDER = ["rose_mist", "amber", "space", "mint", "aurora",
                "coral", "glacier", "evergreen", "graphite", "rose"]
CUSTOM_ID = "custom"
LOW_SAT_THRESHOLD = 0.12   # 饱和度 ≤12% 视为灰——走中性不染色（Motrix content 规则）
DEFAULT_THEME = "dark"
DEFAULT_SCHEME = "graphite"
DEFAULT_CUSTOM = "#737373"


def _hex_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(hex_a: str, hex_b: str, ratio_b: float) -> str:
    """hex_a 与 hex_b 按 ratio_b 混合（ratio_b: 0-1 为 b 占比）。"""
    a, b = _hex_rgb(hex_a), _hex_rgb(hex_b)
    m = tuple(round(av + (bv - av) * ratio_b) for av, bv in zip(a, b))
    return "#%02x%02x%02x" % m


def _saturation(hex_color: str) -> float:
    """HSL 饱和度 0-1（用于低饱和中性判定）。"""
    r, g, b = (v / 255 for v in _hex_rgb(hex_color))
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0
    return d / (1 - abs(mx + mn - 1)) if mx + mn != 0 else 0.0


def _luminance(hex_color: str) -> float:
    """近似亮度 0-1（决定 accent 上文字用黑/白）。"""
    r, g, b = (v / 255 for v in _hex_rgb(hex_color))
    return 0.299 * r + 0.587 * g + 0.114 * b


def derive_accent(seed: str, is_dark: bool) -> dict:
    """seed → accent 系派生值（深浅各自适配）。低饱和种子返回中性（不染色）。"""
    if _saturation(seed) <= LOW_SAT_THRESHOLD:
        # 岩石灰/近灰自定义——accent 走中性文字色，soft 底照常（灰雾）
        base = seed
        hover = _mix(seed, "#ffffff" if is_dark else "#000000", 0.12)
        soft_alpha = 0.10 if is_dark else 0.08
        on = "#ffffff" if is_dark else "#000000"
        return {"accent": base, "accent_hover": hover,
                "accent_soft": f"rgba({_hex_rgb(base)[0]}, {_hex_rgb(base)[1]}, {_hex_rgb(base)[2]}, {soft_alpha:.2f})",
                "accent_on": on, "neutral": True}
    # 正常彩色 seed：hover 向白混（深色模式）/向黑混（浅色模式提深一档？浅色 hover 应变深）
    blend_to = "#ffffff" if is_dark else "#000000"
    hover = _mix(seed, blend_to, 0.15)
    active = _mix(seed, blend_to, 0.28)
    soft_alpha = 0.12 if is_dark else 0.10
    r, g, b = _hex_rgb(seed)
    return {
        "accent": seed,
        "accent_hover": hover,
        "accent_active": active,
        "accent_soft": f"rgba({r}, {g}, {b}, {soft_alpha:.2f})",
        # accent 上文字：亮 seed 用深字，暗 seed 用白字
        "accent_on": "#1b1b1f" if _luminance(seed) > 0.62 else "#ffffff",
        "neutral": False,
    }


def resolve_palette(theme_mode: str = "dark", scheme_id: str | None = None,
                     custom_hex: str | None = None, is_system_dark: bool | None = None) -> dict:
    """主题模式 + 配色 → 完整色板 dict（QSS 生成与 UI 预览共用）。

    theme_mode: "dark"/"light"/"auto"（auto 时由 is_system_dark 决定）
    scheme_id: SCHEMES 键或 CUSTOM_ID；None → DEFAULT_SCHEME
    """
    if theme_mode == "auto":
        theme_mode = "dark" if is_system_dark else "light"
    elif theme_mode not in ("dark", "light"):
        theme_mode = DEFAULT_THEME
    dark = theme_mode == "dark"

    n = NEUTRAL[theme_mode]
    pal = {
        "theme_mode": theme_mode,
        "scheme_id": scheme_id or DEFAULT_SCHEME,
        # 中性层（模板语义名）
        "main_bg": n["window_bg"], "sidebar_bg": n["sidebar_bg"],
        "card_bg": n["card_bg"], "hover_bg": n["hover_bg"],
        "card_border": n["border"], "border_hi": n["border_hi"],
        "text_primary": n["text_primary"], "text_secondary": n["text_secondary"],
        "text_muted": n["text_muted"], "input_bg": n["input_bg"],
        "log_bg": n["log_bg"], "log_text": n["log_text"],
        "highlight": n["highlight"],
        "green": n["success"], "orange": n["warning"], "red": n["danger"],
        "danger_bg": n["danger_bg"],
        # hover/文字派生（QSS greenBtn/dangerBtn 用）
        "green_hover": _mix(n["success"], "#ffffff" if dark else "#000000", 0.15),
        "danger_hover": _mix(n["danger"], "#ffffff" if dark else "#000000", 0.18),
        "danger_text": _mix(n["danger"], "#ffffff" if dark else "#000000",
                            0.35 if dark else 0.12),
    }
    sid = pal["scheme_id"]
    seed = custom_hex if sid == CUSTOM_ID else SCHEMES.get(sid, {}).get("seed", DEFAULT_CUSTOM)
    if not seed or seed == DEFAULT_CUSTOM and sid not in SCHEMES:
        seed = DEFAULT_CUSTOM
    a = derive_accent(seed, dark)
    pal.update(a)
    return pal
