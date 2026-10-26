# -*- coding: utf-8 -*-
"""「她怎么想的」九段流程 + 全参数闸门（2026-10-25 第 17 轮 ④ 扩展）。

## 这组用例防的是什么

主人第一版反馈：「你只是做到了我说的那部分，呈现方式也很简单。」随后给出四条：
① 校验「完整流程」的完整性；② 暴露背景块/记忆块等**参数**；③ 后端参数不止这些，
要梳理清楚一并公开；④ 彻底的前端可视。

对着真实链路逐段核对后，七段扩成九段（补 `routing` 路由与窗口、
把 `send` 拆出 `feedback`），每段加 `params` 明细：

| 阶段 | 原料 | 这轮新暴露的参数 |
|---|---|---|
| 1 inbound 收到消息 | `chat_log` | chat_log id / 全文长度 / 会话 |
| 2 routing 路由与窗口 | `decision_runs` | 决策动作/状态/起止/工具数 |
| 3 context 系统提示 | `prompt_digest` | system 段清单 + 历史条数/字符 |
| 4 inject 背景注入 | 新落的 `digest.backgrounds` | **预算 + 每块三态 + 内容预览** |
| 5 recall 记忆召回 | 同上 | **记忆块状态/token + 逐条记忆** |
| 6 tools 工具调用 | `tool_steps_json` | 每个工具的完整入参/返回 |
| 7 generate 生成 | `turn_traces` | 模型/思考/耗时 + **token 各段账本** |
| 8 send 发送 | `decision_runs` + `feedback` | 决策/正文全文 |
| 9 feedback 对方反应 | `feedback` | 情绪/置信度/延迟/方向核验 |

## 为什么「没数据」也要出段

主人要「完整承载一条消息的流程」——**流程的骨架不能因为某段没采集就消失**。
空段带 `note` 说明为什么空，这本身就是最有用的排查信息（第 47 条教训）。

## 阳性对照（教训 #30/#37）

- `memory_injected` 恒写 True → `test_memory_block_missing_is_reported` 红；
- inject 段 `ok` 恒 True → `test_no_background_blocks_is_reported` 红；
- `tool_ms` 不累加 → `test_tool_steps_are_listed_with_ms` 红；
- 背景块 params 只列保留的（不列被裁/淘汰）→ `test_injected_blocks_show_three_states` 红。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.console_api import (  # noqa: E402
    _build_turn_flow,
    _summarize_turn_flow,
)

PHASE_KEYS = [
    "inbound", "routing", "context", "inject", "recall",
    "tools", "generate", "send", "feedback",
]

#: 段字段集（前端按这套渲染；`blocks`/`entries` 是 inject/recall 的专用结构）
#:
#: 2026-10-25 二次整改加进来的六个「教学字段」：
#: `group` 阶段归属 / `role` 这站干什么 / `logic` 判断依据 /
#: `did` 这轮实际发生了什么 / `status` 四态 / `carries` 交给下一站的东西。
PHASE_FIELDS = {
    "phase", "key", "label", "ok", "ms", "items", "params", "note",
    "group", "role", "logic", "did", "status", "carries",
}

#: 四态（`skip` 不是故障——这一轮本来就不该有）
STATUSES = {"ok", "warn", "error", "skip"}

#: 阶段分组（前端按它画泳道）——顺序就是流程顺序
GROUPS = ["进入", "理解", "决策", "输出"]


def _digest():
    """一次「什么都有」的回合摘要（数字取自 2026-10-25 真实 trace #147/148）。"""
    return {
        "system_chars": 3314, "user_chars": 1415,
        "history_count": 14, "history_chars": 506, "total_chars": 5235,
        "system_sections": ["你是谁", "性格", "说话", "称呼", "此刻"],
        "user_bg_labels": ["关系档案", "协议", "记忆", "窗口", "缺口"],
        "memory_marks": {"corrected": 1, "uncertain": 2},
        "token_total": 5235, "token_limit": 6000,
        "token_sections": {"system": 2400, "history": 380, "user": 1050, "tools": 1405},
        # 2026-10-25 第 17 轮新落库：背景注入的完整参数
        "backgrounds": {
            "budget": {"limit": 1100, "used": 683,
                       "protected_overflow": False, "evidence_reserve_used": False},
            "gap_notice": True,
            "blocks": [
                {"label": "关系档案", "priority": 4, "tokens": 240,
                 "status": "kept", "preview": "林晚 22岁 学生"},
                {"label": "协议", "priority": 1, "tokens": 30,
                 "status": "kept", "preview": "引用协议"},
                {"label": "记忆", "priority": 7, "tokens": 35,
                 "tokens_before": 156, "status": "truncated",
                 "preview": "💭 答应她周末去看画展\n💭 她怕黑"},
                {"label": "记忆", "priority": 7, "tokens": 0,
                 "tokens_before": 90, "status": "dropped", "preview": ""},
                {"label": "反思", "priority": 7, "tokens": 0,
                 "tokens_before": 70, "status": "dropped", "preview": ""},
            ],
        },
    }


def _full_payload():
    return dict(
        message={
            "id": 301785, "content": "（桌面端自检）你在吗",
            "timestamp": "2026-10-25 15:28:38", "speaker": "2524950083",
            "scope": "private:2524950083",
        },
        digest=_digest(),
        prompt_chars=5235,
        trace={
            "model": "deepseek-v4.1-flash", "reasoning_chars": 860,
            "duration_ms": 7201,
            "tools": [{"name": "search_memories", "args": '{"query":"钥匙"}',
                       "result": "3 条", "ms": 120},
                      {"name": "sing", "args": '{"song":"稻香"}', "result": "ok", "ms": 30}],
        },
        decision={"decision": "reply", "status": "completed",
                  "tool_calls": [], "started_at": "2026-10-25 15:28:38",
                  "finished_at": "2026-10-25 15:28:45"},
        feedback={"bot_reply": "在的。", "reply_ms": 3480, "sentiment": "neutral",
                  "confidence": 0.75, "user_reaction": "没事就是问问",
                  "direction_verified": True, "timestamp": "2026-10-25 15:29:00"},
    )


def _by_key(flow):
    return {p["key"]: p for p in flow}


# ── 骨架 ──────────────────────────────────────────────────────────

def test_nine_phases_in_order():
    flow = _build_turn_flow(**_full_payload())
    assert [p["key"] for p in flow] == PHASE_KEYS, (
        "九段的顺序/命名变了——前端时间轴和排查脚本都靠它")
    assert [p["phase"] for p in flow] == list(range(1, 10))
    for p in flow:
        assert PHASE_FIELDS <= set(p), f"{p['key']} 段缺字段：{PHASE_FIELDS - set(p)}"
        assert isinstance(p["items"], list) and isinstance(p["params"], list)
        assert isinstance(p["ok"], bool)
        assert isinstance(p["note"], str)
        # 四态与分组：这是「哪个环节没坏」的判据，不能空
        assert p["status"] in STATUSES, f"{p['key']} 段 status 非法：{p['status']}"
        assert p["group"] in GROUPS, f"{p['key']} 段分组非法：{p['group']}"
        # `ok` 必须与 `status` 一致（skip 不是故障 → ok=True）
        assert p["ok"] == (p["status"] in ("ok", "skip")), (
            f"{p['key']} 段的 ok 和 status 打架：{p['ok']} / {p['status']}")
        # 小白运营者看的三句解释：职责 / 判断依据 / 实际发生了什么
        assert p["role"].strip(), f"{p['key']} 段没说自己在干什么"
        assert p["logic"].strip(), f"{p['key']} 段没说判断依据"
        assert p["did"].strip(), f"{p['key']} 段没给结论（哪怕只写「未采集」）"
        # 环节之间怎么配合：交给下一站的东西
        assert isinstance(p["carries"], list)
        assert p["carries"], f"{p['key']} 段没说交给下一站什么"


def test_groups_are_contiguous_in_order():
    """阶段分组必须是**连续**的：进入 → 理解 → 决策 → 输出。

    若某段分组跳来跳去（例如 inject 标成「决策」），前端画泳道会出现
    「同一阶段被拆成两截」——图谱比时间轴更难自查，所以钉在数据层。
    """
    flow = _build_turn_flow(**_full_payload())
    groups = [p["group"] for p in flow]
    seen: list[str] = []
    for g in groups:
        if not seen or seen[-1] != g:
            assert g not in seen, f"分组「{g}」不连续：{groups}"
            seen.append(g)
    assert seen == GROUPS[:len(seen)], f"分组顺序不对：{seen}"


def test_every_param_has_a_short_explanation():
    """「必要的参数解释（简短）」——每个参数都必须带 `doc`。

    没有解释的参数等于没说：小白看到「预算上限 1100t」不知道自己该不该在意。
    `doc` 同时要短（一句），免得界面被解释淹没。
    """
    flow = _build_turn_flow(**_full_payload())
    for p in flow:
        for it in p["params"]:
            doc = str(it.get("doc") or "").strip()
            assert doc, f"{p['key']} 段的参数「{it.get('k')}」没有解释"
            assert 6 <= len(doc) <= 80, (
                f"{p['key']} 段的参数「{it.get('k')}」解释长度不像是「一句话」：{len(doc)} 字")


def test_full_turn_carries_every_stage():
    flow = {p["key"]: p for p in _build_turn_flow(**_full_payload())}
    assert "桌面端自检" in flow["inbound"]["items"][0]["v"]
    assert flow["routing"]["ok"] is True
    assert flow["context"]["params"][0]["v"].count("、") == 4      # 5 个 system 段
    assert "14 条" in str(flow["context"]["items"])
    # 记忆块被裁 = **打折**，不是故障：她能看见记忆，只是只剩开头。
    # 四态就是为了把这两件事分开——旧版只有布尔，被裁会被误报成红。
    assert flow["recall"]["status"] == "warn", flow["recall"]["status"]
    assert "被裁" in flow["recall"]["did"], flow["recall"]["did"]
    assert flow["generate"]["params"][1]["v"] == "860 字"
    assert flow["send"]["params"][0]["v"] == "reply"
    assert flow["feedback"]["ok"] is True
    bad = {k: p["note"] for k, p in flow.items() if p["status"] == "error"}
    assert not bad, f"这个回合不该有异常段：{bad}"


def test_station_text_is_plain_text():
    """面向用户的文案必须是纯文本——不许混 Markdown。

    前端用 `textContent` 渲染（不跑 Markdown），放了 `**粗体**` 或反引号，
    用户看到的就是一堆星号。这条是二次整改**实测截图**发现的第一处缺陷：
    「这一站**不判断要不要回**」原样显示在了界面里。
    """
    flow = _build_turn_flow(**_full_payload())
    bad: list[tuple[str, str, str]] = []
    for p in flow:
        for name in ("role", "logic", "did", "note"):
            text = str(p.get(name) or "")
            if "**" in text or "`" in text:
                bad.append((p["key"], name, text))
        for c in p["carries"]:
            if "**" in c or "`" in c:
                bad.append((p["key"], "carries", c))
        for it in p["params"]:
            doc = str(it.get("doc") or "")
            if "**" in doc or "`" in doc:
                bad.append((p["key"], "doc", doc))
    assert not bad, f"这些面向用户的文案混进了 Markdown，界面会显示成星号：{bad}"


# ── ④ 的核心：背景注入参数 ────────────────────────────────────────

def test_injected_blocks_show_three_states():
    """保留 / 被裁 / 被淘汰——三态都要出现在 params 里。

    只列「保留了什么」等于把丢掉的那部分藏起来（第 40 条教训：
    体积纪律只管不超预算，看不见「同预算装了多少」）。
    """
    flow = {p["key"]: p for p in _build_turn_flow(**_full_payload())}
    params = {it["k"]: it["v"] for it in flow["inject"]["params"]}
    assert params["预算上限"] == "1100t"
    assert params["已用"].startswith("683t")
    assert params["保护块超顶"] == "否"
    assert params["证据保底"] == "未动用"
    assert params["缺口告知"].startswith("已注入")
    # 三态各至少一条
    values = " ".join(params.values())
    assert "保留" in values and "被裁" in values and "被淘汰" in values
    # 被裁的那块要带「裁前→裁后」
    assert any("156→35t" in v for v in params.values()), params
    # 结构化 blocks 原样透传（界面画表格用）
    blocks = flow["inject"]["blocks"]
    assert len(blocks) == 5
    assert {b["status"] for b in blocks} == {"kept", "truncated", "dropped"}
    assert blocks[2]["preview"].startswith("💭"), "块预览必须能看出里面有什么"


def test_memory_entries_are_listed():
    """记忆块要拆成逐条——主人明确要「记忆召回的记忆块参数」。"""
    flow = {p["key"]: p for p in _build_turn_flow(**_full_payload())}
    entries = flow["recall"]["entries"]
    assert len(entries) == 2, entries
    assert entries[0].startswith("💭 答应她周末去看画展")
    assert entries[1].startswith("💭 她怕黑")
    params = {it["k"]: it["v"] for it in flow["recall"]["params"]}
    assert params["记忆块状态"] == "truncated"      # 记忆块被裁也要如实说
    assert params["占用 token"] == "35t"
    assert params["解析出的条目"] == "2 条"


def test_token_ledger_is_exposed():
    flow = {p["key"]: p for p in _build_turn_flow(**_full_payload())}
    params = {it["k"]: it["v"] for it in flow["generate"]["params"]}
    # ⚠ 不带「/ 预算」：token_total 是全部输入，budget_limit 只是 system 那一份，
    # 两个口径放一起会显示成「超支 54%」的假象（2026-10-25 实测踩到）。
    assert params["token 合计"] == "5235t（估算）"
    assert params["token · system"] == "2400t"
    assert params["token · tools"] == "1405t"


def test_tool_params_carry_full_args_and_result():
    flow = {p["key"]: p for p in _build_turn_flow(**_full_payload())}
    tools = flow["tools"]
    assert len(tools["items"]) == 2
    assert tools["ms"] == 150, "工具耗时必须累加"
    assert len(tools["params"]) == 2
    first = tools["params"][0]["v"]
    assert "入参" in first and "返回" in first and "搜索" not in first
    assert tools["params"][0]["k"].startswith("1. search_memories")


# ── 空段与降级 ────────────────────────────────────────────────────

def test_no_tools_says_so():
    payload = _full_payload()
    payload["trace"] = dict(payload["trace"], tools=[])
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    assert flow["tools"]["items"] == []
    assert "没调工具" in flow["tools"]["note"]


def test_memory_block_missing_is_reported():
    payload = _full_payload()
    digest = _digest()
    digest["backgrounds"]["blocks"] = [
        b for b in digest["backgrounds"]["blocks"] if b["label"] != "记忆"]
    digest["user_bg_labels"] = ["协议", "窗口", "自忆"]
    payload["digest"] = digest
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    assert flow["recall"]["ok"] is False
    assert flow["recall"]["entries"] == []
    kv = {it["k"]: it["v"] for it in flow["recall"]["items"]}
    assert kv["记忆块"] == "未进 prompt", kv


def test_no_background_blocks_is_reported():
    payload = _full_payload()
    digest = _digest()
    digest["user_bg_labels"] = []
    digest["backgrounds"]["blocks"] = []
    payload["digest"] = digest
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    assert flow["inject"]["ok"] is False
    assert "注入链可能整条断了" in flow["inject"]["note"]


def test_old_trace_without_digest_degrades_gracefully():
    """2026-10-25 之前的回合没有 prompt_digest——不能抛异常，要说明「没采集」。"""
    payload = _full_payload()
    payload["digest"] = None
    payload["prompt_chars"] = 0
    flow = _by_key(_build_turn_flow(**payload))
    assert list(flow) == PHASE_KEYS
    for key in ("context", "inject", "recall"):
        assert flow[key]["ok"] is False, (
            f"{key} 段把『没采集』报成了绿——界面上就看不见这个缺口")
        assert "没留 prompt 摘要" in flow[key]["note"], (
            f"{key} 段在『没采集』和『采集到但为空』之间没说清楚")
    assert flow["inbound"]["ok"] and flow["generate"]["ok"]


def test_empty_turn_does_not_crash():
    flow = _build_turn_flow(message=None, digest=None, prompt_chars=0,
                            trace=None, decision=None, feedback=None)
    assert len(flow) == 9
    assert [p["key"] for p in flow] == PHASE_KEYS
    assert all(p["items"] == [] for p in flow)
    assert all(p["note"] for p in flow), "每一段都必须解释自己为什么空"


def test_long_text_is_clipped():
    payload = _full_payload()
    payload["message"] = dict(payload["message"], content="很" * 500)
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    value = flow["inbound"]["items"][0]["v"]
    assert len(value) <= 161 and value.endswith("…")


# ── 四态：「未触发」不是故障（2026-10-25 二次整改）────────────────

def test_skip_is_not_a_failure():
    """没调工具 / 对方没回话 → `skip`，**不是红**。

    旧版这里是「没工具就绿、没反馈就红」，两种都不对：把「没调工具」标绿
    掩盖了「她一次工具都没用过」这个可观察事实；把「对方还没回」标红
    会让人去修一个不存在的 bug。
    """
    payload = _full_payload()
    payload["trace"] = dict(payload["trace"], tools=[])
    payload["feedback"] = None
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    assert flow["tools"]["status"] == "skip"
    assert flow["tools"]["ok"] is True, "skip 段 ok 必须为 True——否则界面会误标红"
    assert flow["feedback"]["status"] == "skip"
    assert flow["feedback"]["ok"] is True
    assert "不是故障" in flow["tools"]["did"]


def test_decision_skip_marks_send_as_skip():
    """她这一轮决定沉默 → 发送段是 skip，不是「正文 0 字」的故障。"""
    payload = _full_payload()
    payload["decision"] = dict(payload["decision"], decision="skip")
    payload["feedback"] = None
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    assert flow["send"]["status"] == "skip", flow["send"]["status"]
    assert "沉默" in flow["send"]["did"]


def test_recall_truncated_is_warn_not_error():
    """被裁是「打折」，被淘汰/未进 prompt 也是打折，**只有没采集才是异常**。

    这个区分直接决定运营者看到的是黄灯还是红灯：黄灯=可以接受，红灯=要修。
    """
    payload = _full_payload()
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    assert flow["recall"]["status"] == "warn", (
        f"记忆块被裁必须是「打折」而不是「{flow['recall']['status']}」"
        "——四态退化成布尔会让人去修一个不存在的 bug")
    digest = _digest()
    digest["backgrounds"]["blocks"] = [
        b for b in digest["backgrounds"]["blocks"] if b["label"] != "记忆"]
    digest["user_bg_labels"] = ["协议", "窗口", "自忆"]
    payload["digest"] = digest
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    assert flow["recall"]["status"] == "warn", (
        "记忆块整块没进 prompt 也是打折（材料可缺），不是异常")
    payload["digest"] = None
    flow = {p["key"]: p for p in _build_turn_flow(**payload)}
    assert flow["recall"]["status"] == "error", (
        "没采集到 prompt 摘要必须报「异常」——这是数据缺口，不是打折")


# ── 体检结论（顶部横幅）─────────────────────────────────────────

def _all_ok_payload():
    """一个真的「全绿」回合：记忆块完整保留、工具与反馈都在。"""
    payload = _full_payload()
    digest = _digest()
    digest["backgrounds"]["blocks"] = [
        {"label": "关系档案", "priority": 4, "tokens": 240,
         "status": "kept", "preview": "林晚 22岁 学生"},
        {"label": "记忆", "priority": 7, "tokens": 110,
         "status": "kept", "preview": "💭 答应她周末去看画展"},
    ]
    digest["user_bg_labels"] = ["关系档案", "记忆"]
    payload["digest"] = digest
    return payload


def test_summary_is_all_green_when_nothing_broken():
    flow = _build_turn_flow(**_all_ok_payload())
    summary = _summarize_turn_flow(flow)
    assert summary["total"] == 9
    assert summary["error"] == 0 and summary["warn"] == 0
    assert summary["ok"] + summary["skip"] == 9
    assert "全部正常" in summary["verdict"]
    assert summary["focus_key"] == ""


def test_summary_points_at_first_break():
    """有异常时结论必须点名**第一个**出问题的站——运营者一次只能修一处。"""
    payload = _all_ok_payload()
    payload["digest"] = None          # context/inject/recall 三段一起变异常
    flow = _build_turn_flow(**payload)
    summary = _summarize_turn_flow(flow)
    assert summary["error"] >= 3
    assert summary["focus_key"] == "context", summary["focus_key"]
    assert "第 3 站「系统提示与历史」异常" in summary["verdict"], summary["verdict"]
    assert "另有" in summary["verdict"], "多处异常时要说还有几处，别让人以为只有一处"
    assert [b["key"] for b in summary["broken"]][:3] == ["context", "inject", "recall"]


def test_summary_warn_only_says_no_fault():
    """只有打折没有异常时，措辞必须是「没有故障」——别把黄灯写成红灯。"""
    flow = _build_turn_flow(**_full_payload())   # 记忆块被裁 → recall 打折
    summary = _summarize_turn_flow(flow)
    assert summary["error"] == 0
    assert summary["warn"] >= 1
    assert "没有故障" in summary["verdict"], summary["verdict"]
    assert summary["focus_key"], "打折也要给出该先看哪一站"
    assert summary["broken"], "打折扣的站要能被列出"


def test_summary_of_empty_turn_does_not_crash():
    flow = _build_turn_flow(message=None, digest=None, prompt_chars=0,
                            trace=None, decision=None, feedback=None)
    summary = _summarize_turn_flow(flow)
    assert summary["total"] == 9
    assert summary["verdict"]
    assert summary["error"] > 0, "什么都没有的回合不可能是全绿"
