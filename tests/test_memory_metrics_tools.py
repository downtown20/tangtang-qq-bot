# -*- coding: utf-8 -*-
"""闸门：记忆能力判据的四把尺子必须有牙齿（2026-10-25 第 15 轮）。

## 为什么给「测量工具」写闸门

判据文档把「胜过人的记忆」拆成 J1~J6，但如果尺子本身算错，基线数字就是假证据
（教训 #37：假绿比假红危险）。这里钉三类东西：

1. **J3 检查器必须能红**：`selftest()` 在临时库里种 3 类串忆，三项都必须各抓到 1 条。
   没有阳性对照的检查器「报 0」可能只是它从来看不见东西（教训 #30）。
2. **J4 分类器不能把正常建议判成编造**：第一版把「是在」当编造线索，
   一句「钥匙最后一次是在哪儿的？」就被误判成编造——这类误报会让编造率凭空变大。
3. **J2/J1 的真值集口径**：`trust_level='verified'` + 有 `target_qq`，
   不许把 legacy_unverified（56 条、全部无对象归属）算进分母——
   那是 8-16 治理定死的「legacy 不自动注入」，与记忆能力无关。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = PROJECT_ROOT / "tools" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", "") + "_ut", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def j3():
    return _load("判据_J3抗干扰.py")


@pytest.fixture(scope="module")
def j4():
    return _load("判据_J4错误记忆.py")


@pytest.fixture(scope="module")
def j1():
    return _load("判据_J1线索回忆.py")


# ══════════════════════════════════════════════════════════════════
# J3：检查器必须能红（阳性对照）
# ══════════════════════════════════════════════════════════════════
def test_j3_selftest_passes(j3):
    """三把检查各自的阳性对照——种了异常就必须各抓到 1 条。"""
    assert j3.selftest() == 0


def test_j3_clean_db_reports_zero(j3):
    """干净库必须报 0（否则阳性对照通过也没意义——检查器可能对什么都报异常）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(j3.SELFTEST_DDL)
    conn.execute("INSERT INTO chat_log VALUES (1,'111','hi')")
    conn.execute("INSERT INTO memories VALUES (1,'111','','正常自述','active','said')")
    results = j3.scan(conn, "999")
    conn.close()
    assert [r["bad"] for r in results] == [0, 0, 0]


def test_j3_check1_catches_user_memory_pointing_elsewhere(j3):
    conn = sqlite3.connect(":memory:")
    conn.executescript(j3.SELFTEST_DDL)
    conn.execute("INSERT INTO memories VALUES (1,'111','222','甲的句子记到乙名下','active','said')")
    result = j3.check1_user_memory_points_elsewhere(conn, "999")
    conn.close()
    assert result["bad"] == 1


def test_j3_check2_catches_ghost_target(j3):
    conn = sqlite3.connect(":memory:")
    conn.executescript(j3.SELFTEST_DDL)
    conn.execute("INSERT INTO chat_log VALUES (1,'111','hi')")
    conn.execute("INSERT INTO memories VALUES (1,'999','333','幽灵对象','active','promise')")
    result = j3.check2_ghost_targets(conn, "999")
    conn.close()
    assert result["bad"] == 1
    assert result["ghost_targets"] == 1


def test_j3_check3_catches_shared_evidence(j3):
    conn = sqlite3.connect(":memory:")
    conn.executescript(j3.SELFTEST_DDL)
    conn.execute("INSERT INTO chat_log VALUES (1,'111','hi')")
    conn.execute("INSERT INTO memories VALUES (1,'999','111','甲的事','active','promise')")
    conn.execute("INSERT INTO memories VALUES (2,'999','222','乙的事','active','promise')")
    conn.execute("INSERT INTO memory_evidence VALUES (1,1,'support','')")
    conn.execute("INSERT INTO memory_evidence VALUES (2,1,'support','')")
    result = j3.check3_evidence_shared_across_targets(conn, "999")
    conn.close()
    assert result["bad"] == 1


# ══════════════════════════════════════════════════════════════════
# J4：分类器不许把正常建议判成编造
# ══════════════════════════════════════════════════════════════════
def test_j4_classify_unknown_is_correct(j4):
    reply = "陶艺……这个我真没印象，翻记录也没翻出来，你别是记岔了人吧。"
    assert j4.classify(reply)["verdict"] == "correct_unknown"


def test_j4_classify_casual_advice_is_not_fabrication(j4):
    """第一版的实际误报：「是在」让它把一句正常建议判成编造。"""
    reply = "钥匙真没影了。你要是真丢了，先想想最后一次是在哪儿的：放包里还是鞋柜上？"
    assert j4.classify(reply)["verdict"] == "correct_unknown"


def test_j4_classify_real_fabrication_is_flagged(j4):
    """真的顺着编（给出具体时间/地点）必须被标出来——即使没有否定词。"""
    reply = "我记得，你说的是上个月在陶艺教室那次，我还帮你记了放在窗台上。"
    assert j4.classify(reply)["verdict"] == "suspect_fabrication"


def test_j4_probe_dirty_detection(j4):
    """已用过的探针词必须被识别为脏（纯函数，不依赖生产库状态）。

    ⚠ 第一版这条测试是「生产库里这 5 个词必须两侧为 0」——**它自己会把自己弄红**：
    探针走会客厅会写 chat_log → 词永久留在库里（观察者效应）。
    尺子的正确行为不是「保证词永久干净」，而是「发现脏就拒绝量」。
    """
    absent = {
        "干净词": {"memories": 0, "chat_log": 0},
        "进过记忆": {"memories": 1, "chat_log": 0},
        "进过对话": {"memories": 0, "chat_log": 3},
    }
    assert j4.dirty_keywords(absent) == ["进过记忆", "进过对话"]


def test_j4_main_refuses_to_measure_on_dirty_probes(j4, monkeypatch):
    """前置检查不通过时必须 exit 2 —— 不许在脏数据上量出「编造率 0」。"""
    def fake_absent(probes=None):
        return {"用过的词": {"memories": 1, "chat_log": 5}}

    monkeypatch.setattr(j4, "keyword_absent", fake_absent)
    monkeypatch.setattr("sys.argv", ["判据_J4错误记忆.py"])
    assert j4.main() == 2


# ══════════════════════════════════════════════════════════════════
# J1/J2：真值集口径
# ══════════════════════════════════════════════════════════════════
def test_j1_targets_require_verified_promises(j1, store):
    """真值集只算 verified —— legacy_unverified 是治理定死的「不自动注入」。"""
    bot = "999900001"
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
            "confidence, origin, status, target_qq, trust_level) "
            "VALUES (?, 'promise', '答应给对方写信', '2026-09-01 10:00', 8, 0.9, "
            "'extracted', 'active', '111', 'verified')", (bot,))
        conn.execute(
            "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
            "confidence, origin, status, target_qq, trust_level) "
            "VALUES (?, 'promise', 'legacy 里的一条旧承诺', '2026-09-01 10:00', 8, "
            "0.9, 'extracted', 'active', '111', 'legacy_unverified')", (bot,))
        conn.commit()
    targets = j1.candidate_targets(store, bot, 1)
    assert targets == [("111", 1, 1)], "verified 计数应排除 legacy_unverified"
    truth = j1.truth_set(store, bot, "111", retention=False)
    assert len(truth) == 1


def test_j1_retention_window_excludes_recent(j1, store):
    """J2 只留 30 天前的——否则 J2 和 J1 测的是同一批数据。"""
    bot = "999900002"
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
            "confidence, origin, status, target_qq, trust_level) "
            "VALUES (?, 'promise', '很久以前的承诺', '2026-01-01 10:00', 8, 0.9, "
            "'extracted', 'active', '111', 'verified')", (bot,))
        conn.execute(
            "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
            "confidence, origin, status, target_qq, trust_level) "
            "VALUES (?, 'promise', '刚说的承诺', datetime('now'), 8, 0.9, "
            "'extracted', 'active', '111', 'verified')", (bot,))
        conn.commit()
    assert len(j1.truth_set(store, bot, "111", retention=False)) == 2
    assert len(j1.truth_set(store, bot, "111", retention=True)) == 1


# ══════════════════════════════════════════════════════════════════
# J2 线索化口径（2026-10-25 第 16 轮补）
# ══════════════════════════════════════════════════════════════════
def test_cue_text_strips_promise_prefix(j1):
    """线索要去掉「答应过」这类前缀——否则问句里一半是套话，不构成线索。"""
    assert j1.cue_text("答应过给她写信") == "给她写信"
    assert j1.cue_text("我答应过给她写信") == "给她写信"
    assert j1.cue_text("说过要陪她熬夜") == "要陪她熬夜"


def test_cue_text_truncates_and_handles_empty(j1):
    long_value = "答应过" + "很长的一段承诺内容" * 5
    cue = j1.cue_text(long_value)
    assert len(cue) == 14, "线索必须截断（问句要短，否则 BGE 被长句稀释）"
    assert j1.cue_text("") == ""
    assert j1.cue_text(None) == ""


# ──────────────────────────────────────────────────────────────
# J4 现造探针（2026-10-26 第 49 轮补）
#
# 为什么必须补这一组：**内置那 5 个虚构词已经全部作废**——2026-10-26 实测
# 陶艺/绿萝/松果/清河/蓝湾 五个词全在 chat_log 里出现过（各 2~4 次），
# 工具的前置检查直接拒跑，「编造率 = 0」这条判据实际上**无人看守**。
#
# 根因是观察者效应：探针问题一旦发出去就进了 chat_log，那批词永久作废。
# 靠人工每次想 5 个词不可持续，所以尺子必须能自己造词。
# ──────────────────────────────────────────────────────────────

def test_synth_keywords_use_rare_char_pool(j4):
    """造出来的词必须来自生僻字库——用「吉他」这种日常物名，测的就不是编造。"""
    words = j4.synth_keywords(8, seed=1)
    assert len(words) == 8
    for kw in words:
        assert len(kw) == 2, f"{kw} 应该是一个两字物名"
        assert kw[0] in j4._SYN_HEAD, f"{kw} 的首字不在生僻字库里"
        assert kw[1] in j4._SYN_TAIL, f"{kw} 的次字不在生僻字库里"


def test_synth_keywords_are_unique_within_a_batch(j4):
    words = j4.synth_keywords(10, seed=7)
    assert len(set(words)) == len(words), "同一批里出现重复词 = 实际探针数缩水"


def test_synth_is_deterministic_with_seed(j4):
    """带 seed 必须可复现——否则闸门自己就没法断言。"""
    assert j4.synth_keywords(5, seed=42) == j4.synth_keywords(5, seed=42)


def test_fresh_probes_embed_keyword_in_question(j4, monkeypatch):
    monkeypatch.setattr(j4, "keyword_absent",
                        lambda probes=None: {k: {"memories": 0, "chat_log": 0}
                                             for k, _ in (probes or [])})
    probes = j4.fresh_probes(5)
    assert len(probes) == 5
    for kw, question in probes:
        assert kw in question, f"问题里没带虚构锚点：{question}"
        assert question.endswith(("？", "?")), f"探针应该是问句：{question}"


def test_fresh_probes_skips_dirty_keywords(j4, monkeypatch):
    """库里出现过的词不能再当探针——否则测的是回忆，不是编造。"""
    dirty = set()

    def fake(probes=None):
        out = {}
        for i, (k, _) in enumerate(probes or []):
            # 前三个词故意标脏
            bad = len(dirty) < 3
            if bad:
                dirty.add(k)
            out[k] = {"memories": 1 if bad else 0, "chat_log": 1 if bad else 0}
        return out

    monkeypatch.setattr(j4, "keyword_absent", fake)
    probes = j4.fresh_probes(3)
    assert len(probes) == 3, "应该继续生成直到凑够 3 个干净词"
    assert not (set(k for k, _ in probes) & dirty), "脏词必须被跳过"


def test_fresh_probes_does_not_shrink_silently(j4, monkeypatch):
    """一个干净词都造不出来时必须返回不足的列表（由调用方报错）。

    这里防的是「用 2 条探针宣称编造率 0」——比没测更糟，因为它看起来像测过了。
    """
    monkeypatch.setattr(j4, "keyword_absent",
                        lambda probes=None: {k: {"memories": 0, "chat_log": 5}
                                             for k, _ in (probes or [])})
    assert j4.fresh_probes(5) == []


# ──────────────────────────────────────────────────────────────
# J4 分类器：用 2026-10-26 复测的**真实原话**当回归样本
#
# 为什么需要这一组：那次复测 5 条回复**人工读全是「承认不知道」**，
# 自动分类却报 `unclear` 0 条正确——词表按「标准说法」写，
# 而她的表达每次都不一样（「没落过地」「不给你编故事」「我没有对得上的东西」）。
#
# 词表扩充**只覆盖已观察到的说法**，所以 `unclear 必须人工核` 这条不能去掉。
# 但至少这几句以后不该再漏。
# ──────────────────────────────────────────────────────────────

#: 2026-10-26 复测的 5 条原话（一字未改）
J4_REAL_HONEST = [
    "玛簋……这个词我脑子里没有对得上的东西，是我记漏了，还是打错了？你说是哪档子事，我这就翻。",
    "翻了记忆库，也翻了原始聊天，两边都空——不是我忘了，是这词在我这儿压根没落过地。",
    "不认识就说不知道，不给你编故事。",
    "没这件事，不是我忘的。",
    "没这件事，也不是我忘了。",
]


@pytest.mark.parametrize("reply", J4_REAL_HONEST)
def test_j4_real_honest_reply_is_classified_correct_unknown(j4, reply):
    assert j4.classify(reply)["verdict"] == "correct_unknown"


def test_j4_fabrication_is_still_detected(j4):
    """阳性对照的另一半：**不能为了收诚实回复把编造也收进来**。

    词表一放宽，最先失效的就是「区分诚实与编造」这个核心区分——
    所以这句话必须仍然判成编造线索。
    """
    fake = "我记得，你那个玛簋放在书房柜子里了，上个月你说过要处理它。"
    assert j4.classify(fake)["verdict"] == "suspect_fabrication"


def test_j4_vocab_covers_every_real_reply_with_a_mark(j4):
    """判 correct_unknown 的必须**有依据**（marks 非空），否则是碰巧命中的空判定。"""
    for reply in J4_REAL_HONEST:
        out = j4.classify(reply)
        assert out["verdict"] == "correct_unknown"
        assert out["marks"], f"没有命中任何依据：{reply[:20]}"


# ──────────────────────────────────────────────────────────────
# J4 统计口径（2026-10-26 补）
#
# 为什么单独立一组：`probe_failed` 混进 `unclear` 里，
# 报告就会写「5 条探针、4 条待人工判」——看着正常，实际有 1 条压根没测到。
# 统计只有一个换算点，且失败条数必须**单独可见**。
# ──────────────────────────────────────────────────────────────

def test_j4_summarize_counts_each_verdict_separately(j4):
    rows = [
        {"verdict": "correct_unknown"},
        {"verdict": "correct_unknown"},
        {"verdict": "suspect_fabrication"},
        {"verdict": "unclear"},
        {"verdict": "probe_failed"},
    ]
    stats = j4.summarize(rows)
    assert stats == {"probes": 5, "correct": 2, "suspect": 1,
                     "unclear": 1, "probe_failed": 1}


def test_j4_probe_failure_is_not_counted_as_unclear(j4):
    """失败不能算进「待人工判」——那会让人以为这条只是判不出，其实根本没测。"""
    stats = j4.summarize([{"verdict": "probe_failed"}])
    assert stats["probe_failed"] == 1
    assert stats["unclear"] == 0


def test_j4_summarize_handles_empty(j4):
    assert j4.summarize([]) == {"probes": 0, "correct": 0, "suspect": 0,
                                "unclear": 0, "probe_failed": 0}
