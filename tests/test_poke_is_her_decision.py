"""T2.7（2026-10-16）被戳的回应必须是**她的决定**，不是系统的骰子加写死台词。

改造前 handle_poke：30% 概率反戳（骰子）→ 否则按亲密度从三条**写死的台词**里
random.choice 挑一句 → 再 50% 换成贴图 / 40% 换成 face。四个决定没有一个是她的
（教训 #9 同族：系统替 LLM 做决定）。

现在：把「被戳了」当成一件进她脑子的事（材料＝昵称 / 亲密度档位 / 群里刚在聊什么），
反应由她给（回话 / [戳回去] / [贴图:x] / 空回复＝不理）；系统只负责给材料、发送、记结果。
仅当 LLM 调用失败才走中性降级兜底——她主动不理不走兜底（教训 #27 的反面：
静默不是降级，是丢事件；但把失败伪装成「她说了句话」比说一句「？」更坏）。
"""

import ast
import asyncio
from pathlib import Path

from agent import protocols as _protocols
from agent.handler import MessageHandler

SRC = (Path(__file__).resolve().parents[1] / "agent" / "handler.py").read_text(
    encoding="utf-8")


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════
# A. 源码闸门：骰子与写死台词都不许复活（带阳性对照，教训 #30）
# ═══════════════════════════════════════════════════════

_DICE = {"random", "randint", "choice", "shuffle", "sample", "uniform"}
_CANNED = ("干嘛啦！", "别戳了痒...", "谁戳我", "戳回去！")


def _handle_poke_smells(src: str) -> list[str]:
    """只看 AST 节点（注释/docstring 里提到 random 是说明，不是骰子）。"""
    out: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "handle_poke"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Import) and any(a.name == "random" for a in sub.names):
                out.append("import random")
            elif isinstance(sub, ast.ImportFrom) and sub.module == "random":
                out.append("from random import")
            elif isinstance(sub, ast.Attribute) and sub.attr in _DICE:
                out.append(f".{sub.attr}()")
            elif isinstance(sub, ast.Name) and sub.id in ("random", "_random"):
                out.append(f"name {sub.id}")
            elif (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                  and sub.value in _CANNED):
                out.append("写死台词")
    return out


def test_handle_poke_has_no_dice_and_no_canned_lines():
    assert _handle_poke_smells(SRC) == []


def test_scanner_has_positive_control():
    planted = (
        "class X:\n"
        "    async def handle_poke(self, data):\n"
        "        import random\n"
        "        replies = ['干嘛啦！']\n"
        "        return random.choice(replies)\n"
    )
    hits = _handle_poke_smells(planted)
    assert "import random" in hits
    assert ".choice()" in hits
    assert "写死台词" in hits


def test_handle_poke_is_llm_generated_and_enriched():
    """她的回应必须由 LLM 生成，并且过发送链路（清洗/贴图解析）。"""
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_poke":
            seg = ast.get_source_segment(SRC, node) or ""
            assert "_call_llm_light" in seg
            assert "_enrich_reply_async" in seg
            assert "POKE_BACK_MARKER" in seg
            return
    raise AssertionError("handle_poke 不见了（改名请同步本闸门）")


# ═══════════════════════════════════════════════════════
# B. 行为：她说什么就发什么；不理就不理；失败才兜底
# ═══════════════════════════════════════════════════════

class _NapcatStub:
    def __init__(self):
        self.group: list = []
        self.private: list = []
        self.apis: list = []

    async def send_group_message(self, gid, text):
        self.group.append((gid, text))
        return True

    async def send_private_message(self, uid, text):
        self.private.append((uid, text))
        return True

    async def _call_api(self, api, params):
        self.apis.append((api, params))
        return {"status": "ok"}


def _handler(reply="", raises=False):
    h = object.__new__(MessageHandler)
    h.napcat = _NapcatStub()
    h._group_blacklist = set()
    h.memory = type("M", (), {
        "get_or_create_person": staticmethod(
            lambda qq: {"intimacy": 80, "nickname": "小明"}),
        "short_term": {},
    })()
    h.personality = type("P", (), {"_cached_base": "（人格卡）你是糖糖。"})()
    h.seen = {"system": "", "user": ""}
    h.poke_calls = []

    async def _run_store_io(op, fn, *a, **kw):
        return fn(*a, **kw)

    async def _call_llm_light(system, user):
        h.seen["system"], h.seen["user"] = system, user
        if raises:
            raise RuntimeError("LLM 挂了")
        return reply

    async def _enrich_reply_async(text, user_text="", intimacy=0, group_id=""):
        h.seen["enrich"] = (text, group_id)
        return text

    async def _try_proactive_poke(user_id, group_id, intimacy=None, nickname=""):
        h.poke_calls.append((user_id, group_id, intimacy, nickname))
        return "ok"

    h._run_store_io = _run_store_io
    h._call_llm_light = _call_llm_light
    h._enrich_reply_async = _enrich_reply_async
    h._try_proactive_poke = _try_proactive_poke
    return h


def _poke(h, group_id="12345", user_id="1001"):
    return _run(MessageHandler.handle_poke(h, {"group_id": group_id, "user_id": user_id}))


def test_her_answer_is_sent_verbatim():
    h = _handler(reply="别戳了痒…")
    _poke(h)
    assert h.napcat.group == [("12345", "别戳了痒…")]
    assert h.poke_calls == []          # 只有反戳标记才反戳


def test_poke_back_is_her_choice():
    h = _handler(reply="[戳回去]")
    _poke(h)
    assert h.poke_calls and h.poke_calls[0][0] == "1001"
    assert h.napcat.group == []        # 只反戳，不说话


def test_silence_is_her_choice_not_a_failure():
    h = _handler(reply="")
    _poke(h)
    assert h.napcat.group == [] and h.napcat.private == []
    assert h.poke_calls == []


def test_llm_failure_uses_neutral_fallback_not_a_persona_line():
    h = _handler(raises=True)
    _poke(h)
    assert h.napcat.group == [("12345", _protocols.POKE_DEGRADED_REPLY)]
    assert _protocols.POKE_DEGRADED_REPLY not in _CANNED


def test_private_poke_uses_private_channel():
    h = _handler(reply="嗯？")
    _poke(h, group_id="0")
    assert h.napcat.private == [("1001", "嗯？")]
    assert h.napcat.group == []


def test_private_cannot_poke_back():
    h = _handler(reply="[戳回去]")
    _poke(h, group_id="0")
    assert h.poke_calls == []


def test_materials_are_handed_to_her():
    h = _handler(reply="？")
    h.memory.short_term = {"12345": [{"nickname": "小红", "message": "今晚吃什么"}]}
    _poke(h)
    assert "你是糖糖" in h.seen["system"]          # 人格在前
    assert "小明" in h.seen["user"] and "很熟" in h.seen["user"]
    assert "今晚吃什么" in h.seen["user"]          # 群里刚在聊什么
    assert "不要编" in h.seen["system"]            # 材料外细节不编（教训 #22 同族）


def test_blacklisted_group_is_ignored():
    h = _handler(reply="在的")
    h._group_blacklist = {"12345"}
    _poke(h)
    assert h.napcat.group == []
    assert h.seen["user"] == ""                    # 连 LLM 都没调

