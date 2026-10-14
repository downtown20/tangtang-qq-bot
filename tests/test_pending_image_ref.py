"""P0-B2：图片先到、追问后到可关联原图（2026-08-28 协作任务包，先写红测再实现）

验收点：
  1. 批处理之前捕获图片事件（保留 message_id/url/file_id/hash），供 TTL 内追问
  2. 按会话作用域 + 用户隔离——他人图片/其他会话取不到
  3. TTL 过期不可用；容量有上限（最旧让位）
  4. analyze_image 执行层回退 recent ref（当前回合无图时）——回退结果带来源前缀，
     当前回合图片返回原始描述（旧契约）
  5. 工具过滤：存在 recent 图片时保留 analyze_image（防「没有识图能力」幻觉）
  6. 旧测试替身（无缓存属性 / 无 recent helper）安全返回 None / 安全拒绝，不崩溃
  7. 无 url 的 CQ 图片不捕获（无法分析，不占位）
"""
import asyncio
import ast
import time
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

from agent.handler import MessageHandler


def _handler():
    """无 __init__ 的最小 handler 实例：只挂载 recent 图依赖"""
    h = object.__new__(MessageHandler)
    h._recent_image_refs = defaultdict(lambda: deque(maxlen=6))
    h._recent_image_ttl = 300.0
    h.vision_enabled = True
    return h


def _img_msg(user, mid, url="http://x/a.jpg", file="a.jpg", raw=None):
    return {
        "user_id": str(user),
        "message_id": mid,
        "raw_message": raw or f"[CQ:image,file={file},url={url}]",
    }


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════
# 1. 捕获 + 同会话同用户取最近一张
# ═══════════════════════════════════════════════════════

def test_capture_and_get_recent():
    h = _handler()
    h._capture_recent_image("g1", _img_msg("1001", 777))
    ref = h._get_recent_image_ref("g1", "1001")
    assert ref is not None
    assert ref["message_id"] == 777
    assert ref["url"] == "http://x/a.jpg"
    assert ref["file_id"] == "a.jpg"
    assert ref["scope_id"] == "g1"
    assert ref["user_id"] == "1001"
    assert ref["expires_at"] > ref["time"]  # TTL 生效
    # 多张：取最近一张
    h._capture_recent_image("g1", _img_msg("1001", 778))
    ref2 = h._get_recent_image_ref("g1", "1001")
    assert ref2["message_id"] == 778


# ═══════════════════════════════════════════════════════
# 2. 用户隔离 / 会话隔离
# ═══════════════════════════════════════════════════════

def test_user_isolation():
    h = _handler()
    h._capture_recent_image("g1", _img_msg("1001", 1))
    assert h._get_recent_image_ref("g1", "1002") is None  # 他人图片取不到


def test_scope_isolation():
    h = _handler()
    h._capture_recent_image("g1", _img_msg("1001", 1))
    assert h._get_recent_image_ref("g2", "1001") is None  # 其他会话取不到
    h._capture_recent_image("_private_1001", _img_msg("1001", 2))
    assert h._get_recent_image_ref("_private_1001", "1001")["message_id"] == 2


# ═══════════════════════════════════════════════════════
# 3. TTL 过期 / 容量上限
# ═══════════════════════════════════════════════════════

def test_ttl_expiry():
    h = _handler()
    h._capture_recent_image("g1", _img_msg("1001", 1))
    ref = h._get_recent_image_ref("g1", "1001")
    ref["expires_at"] = time.time() - 1  # 模拟过期
    assert h._get_recent_image_ref("g1", "1001") is None
    # 过期后新图照常可用
    h._capture_recent_image("g1", _img_msg("1001", 2))
    assert h._get_recent_image_ref("g1", "1001")["message_id"] == 2


def test_capacity_limit():
    h = _handler()
    for i in range(8):
        h._capture_recent_image("g1", _img_msg("1001", i))
    assert len(h._recent_image_refs["g1"]) == 6  # 上限 6，最旧 2 张让位
    assert h._get_recent_image_ref("g1", "1001")["message_id"] == 7  # 最近一张


# ═══════════════════════════════════════════════════════
# 4. 旧替身兼容：缺失缓存属性安全返回 None
# ═══════════════════════════════════════════════════════

def test_missing_cache_attr_is_safe():
    """object.__new__ 未初始化缓存的实例：取图安全返回 None，不 AttributeError"""
    h = object.__new__(MessageHandler)  # 无 __init__，无 _recent_image_refs
    assert h._get_recent_image_ref("g1", "1001") is None


# ═══════════════════════════════════════════════════════
# 5. 无 url 不捕获（无法分析，不占位）
# ═══════════════════════════════════════════════════════

def test_no_url_not_captured():
    h = _handler()
    h._capture_recent_image("g1", _img_msg("1001", 1, raw="[CQ:image,file=a.jpg]"))
    assert h._get_recent_image_ref("g1", "1001") is None
    # 非图片消息也不捕获
    h._capture_recent_image("g1", {"user_id": "1001", "message_id": 2, "raw_message": "纯文本"})
    assert h._get_recent_image_ref("g1", "1001") is None


# ═══════════════════════════════════════════════════════
# 6. analyze_image 执行层：当前回合原样、回退带前缀、无 helper 安全拒绝
# ═══════════════════════════════════════════════════════

def test_analyze_current_turn_returns_raw_desc():
    """旧契约：当前回合 image_ref 的 analyze_image 返回原始描述（无来源前缀）"""
    seen = []

    async def vision(url, file_id, prompt=""):
        seen.append((url, file_id, prompt))
        return f"看到了 {url}"

    fake = SimpleNamespace(voice_enabled=True, _call_vision=vision)
    actions = {"respond": True, "image_ref": {
        "url": "https://turn/A.jpg", "file_id": "A",
        "scope_id": "group-1", "user_id": "user-A",
    }}
    result = _run(MessageHandler._execute_tool(
        fake, "analyze_image", {"query": "图里是谁"}, "group-1", "user-A", actions))
    assert result == "看到了 https://turn/A.jpg"
    assert seen == [("https://turn/A.jpg", "A", "图里是谁")]


def test_analyze_recent_fallback_adds_source_prefix():
    """跨回合回退 recent 图片：回执带实际来源（审查 Important 4）"""
    seen = []

    async def vision(url, file_id, prompt=""):
        seen.append((url, file_id, prompt))
        return "描述内容"

    def recent_ref(scope_id, user_id):
        return {"url": "https://recent/B.jpg", "file_id": "B",
                "message_id": 555, "scope_id": scope_id, "user_id": user_id}

    fake = SimpleNamespace(voice_enabled=True, _call_vision=vision,
                           _get_recent_image_ref=recent_ref)
    result = _run(MessageHandler._execute_tool(
        fake, "analyze_image", {"query": "刚才那张图是什么"}, "g1", "1001",
        {"respond": True}))
    assert result == "[分析对象来源: msg_id=555] 描述内容"
    assert seen == [("https://recent/B.jpg", "B", "刚才那张图是什么")]


def test_analyze_without_helper_rejects_safely():
    """旧替身无 recent helper：安全拒绝（不 AttributeError），不读旧实例字段"""
    called = False

    async def vision(*_a, **_k):
        nonlocal called
        called = True
        return "不应看到"

    fake = SimpleNamespace(voice_enabled=True, _call_vision=vision,
                           _pending_image={"url": "https://legacy/secret.jpg"})
    result = _run(MessageHandler._execute_tool(
        fake, "analyze_image", {}, "g2", "user-B", {"respond": True}))
    assert "没有图片" in result
    assert called is False


# ═══════════════════════════════════════════════════════
# 7. 源码契约：捕获在批处理之前；执行层/工具过滤回退存在
# ═══════════════════════════════════════════════════════

def _func_body(name):
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.get_source_segment(src, node)


def test_capture_before_batching_contract():
    """群/私聊入口：图片捕获必须在批处理拦截之前完成"""
    for func, enqueue in [("handle_group_message", "enqueue_group"),
                          ("handle_private_message", "enqueue_private")]:
        body = _func_body(func)
        assert "_capture_recent_image" in body
        assert body.index("_capture_recent_image") < body.index(enqueue)


def test_analyze_falls_back_to_recent_contract():
    """analyze_image 执行层必须有 recent 回退（当前回合无图时）"""
    body = _func_body("_execute_tool")
    assert "_get_recent_image_ref" in body


def test_tool_filter_keeps_analyze_with_recent_contract():
    """工具过滤：recent 图片存在时必须保留 analyze_image"""
    body = _func_body("_build_memory_tools")
    assert "_get_recent_image_ref" in body


def test_no_legacy_pending_image_identifier():
    """旧契约：'_pending_image' 字符串不得出现在 handler.py（命名禁区）"""
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    assert "_pending_image" not in src
