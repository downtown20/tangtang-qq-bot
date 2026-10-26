"""LLM 端点 URL 契约（2026-09-21）。

背景：同一个 base_url 配置项，两处代码的期望原来是**相反**的——主路径
（deepseek / anthropic）拼 `{base}/v1/...`（要求 base 不带 /v1），而
`_call_openai_compatible` 拼 `{base}/chat/completions`（要求 base **必须**带 /v1），
而且六处调用点都不容忍结尾斜杠。

现场：在控制台把 provider 切成 custom 后，base 填 `https://host/`，拼出
`https://host//chat/completions`（双斜杠 + 缺 /v1），服务器回 200 + HTML，
`resp.json()` 抛 `Expecting value: line 1 column 1 (char 0)`——**报错信息里完全
看不出是 URL 拼错了**，只能靠翻日志里 httpx 打的那行才看得见。

现在全部收敛到 `agent.handler._endpoint()`。本测试钉两件事：

A. 行为：各种 base_url 填法（带不带尾斜杠、带不带 /v1）都收敛到同一个正确 URL，
   且**出厂默认值的行为一字不变**（那条是最要紧的回归线）。
B. 闸门：`agent/` 下不得再出现裸 f-string 拼 LLM 端点——新增调用点必须走
   `_endpoint()`，否则这里红灯。唯一例外是 `_endpoint` 自身的函数体。
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
BASE = Path(__file__).parent.parent

CHAT = "/chat/completions"
MSG = "/messages"


# ---------------------------------------------------------------------------
# A. 行为
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base,path,want", [
    # 出厂默认值——行为必须一字不变
    ("https://api.deepseek.com", CHAT, "https://api.deepseek.com/v1/chat/completions"),
    # 以下四种以前全部会拼错
    ("https://api.deepseek.com/", CHAT, "https://api.deepseek.com/v1/chat/completions"),
    ("https://api.deepseek.com/v1", CHAT, "https://api.deepseek.com/v1/chat/completions"),
    ("https://api.deepseek.com/v1/", CHAT, "https://api.deepseek.com/v1/chat/completions"),
    ("https://relay.example/", CHAT, "https://relay.example/v1/chat/completions"),
    ("https://relay.example/v1", CHAT, "https://relay.example/v1/chat/completions"),
    # 另一条路径与带路径前缀的主机
    ("https://api.anthropic.com", MSG, "https://api.anthropic.com/v1/messages"),
    ("https://openrouter.ai/api", MSG, "https://openrouter.ai/api/v1/messages"),
])
def test_endpoint_normalizes(base, path, want):
    from agent.handler import _endpoint
    assert _endpoint(base, path) == want


def test_endpoint_never_double_slash_or_missing_v1():
    """反面对照：任何输入都不该产生 `//`，也不该漏掉 `/v1/`。"""
    from agent.handler import _endpoint
    for base in ["https://h", "https://h/", "https://h/v1", "https://h/v1/",
                 "https://h/a/b/", "https://h/a/b/v1", "https://h/a/b/v1/"]:
        url = _endpoint(base, CHAT)
        assert "//" not in url.split("://", 1)[1], f"{base!r} -> {url}"
        assert "/v1/" in url, f"{base!r} -> {url}"


# ---------------------------------------------------------------------------
# B. 闸门：不许再有裸 f-string 拼端点
# ---------------------------------------------------------------------------

# 例外必须登记并写明理由（本仓惯例）。
ALLOWLIST: dict[tuple[str, str], str] = {
    ("agent/handler.py", "_endpoint"): "它就是唯一的 URL 拼装点，理由见其 docstring",
    # 图库打标的云端视觉 API（2026-10-26）：**不是她的对话端点**。
    # base 是使用者在设置里自己填的通用 OpenAI 兼容地址，不由 `_endpoint()` 管理
    # （那个函数只服务她的 LLM 客户端与 base_url 解析）。让它走 `_endpoint()`
    # 反而会把「打标服务商」和「她的脑子」两件事绑死。
    ("agent/image_tagger.py", "_ask_api"): "图库打标的视觉 API，与她的对话端点无关，base 由使用者配置",

}

ENDPOINT_HINTS = ("/chat/completions", "/messages")
BASE_NAMES = {"base", "base_url"}


def _raw_constructions(path: Path) -> list[tuple[int, str]]:
    """找出「用 f-string 把 base/base_url 直接插进端点 URL」的地方。

    只认**函数体内**的构造——注释和 docstring 不算（它们不是代码）。
    """
    rel = path.relative_to(BASE).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    hits: list[tuple[int, str]] = []

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (rel, func.name) in ALLOWLIST:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.JoinedStr):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if not (names & BASE_NAMES):
                continue
            literal = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if any(hint in literal for hint in ENDPOINT_HINTS):
                hits.append((node.lineno, f"{func.name}() 里拼了 {literal!r}"))
    return hits


def test_no_raw_llm_endpoint_construction():
    offenders: list[str] = []
    for path in sorted((BASE / "agent").glob("*.py")):
        for lineno, desc in _raw_constructions(path):
            offenders.append(f"  {path.relative_to(BASE).as_posix()}:{lineno}  {desc}")
    assert not offenders, (
        "发现裸 f-string 拼 LLM 端点——必须改走 agent.handler._endpoint()：\n"
        + "\n".join(offenders)
    )
