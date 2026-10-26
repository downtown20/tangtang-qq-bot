"""信任门可观测性（P2 立项研究 · 第三期）：界面上的数字必须等于闸门的真实行为。

这一期的全部价值是「显示的数字不撒谎」。所以测试不能只断言「字段存在」——
那只是在测 SQL 能跑。真正的口径一致性只能靠**同一条数据同时走闸门和走统计**
来证明：每个信任等级各造一条记忆 / 一份画像，既问闸门放不放行，也问统计把
它算在哪一侧，两边必须同时给出同一个答案。

`TRUSTED_TRUST_LEVELS` 是这道门的唯一放行集合（store 模块级常量），
`query_memories(trusted_only=True)` 与 `Memory.active_notes()` 都取自它。
参数表里带着 `""`（NULL 经 COALESCE 后的真实状态）和几个不该放行的等级，
是为了让「以后有人偷偷往 SQL 里加一档」能立刻红。
"""

from types import SimpleNamespace

from agent.console_api import ConsoleApi as ConsoleAPI
from agent.memory import MemorySystem
from agent.store import TRUSTED_TRUST_LEVELS


LEVELS = ["verified", "manual", "corrected", "unverified", "legacy_unverified", ""]


def _memory_with_trust(store, qq_id: str, level: str, value: str) -> int:
    """插一条记忆并强改 trust_level（模拟历史数据的各种信任状态）。"""
    memory_id = store.insert_memory(qq_id, "fact", value, importance=6)
    with store._connect() as conn:
        conn.execute("UPDATE memories SET trust_level=? WHERE id=?", (level, memory_id))
        conn.commit()
    return memory_id


def test_recall_gate_only_passes_trusted_levels(store):
    """召回门放行的集合必须**恰好**是 TRUSTED_TRUST_LEVELS（多放少放都红）。"""
    ids = {
        level: _memory_with_trust(store, "30001", level, f"关于{level or '空等级'}的事实")
        for level in LEVELS
    }
    recalled = {
        row["id"]
        for row in store.query_memories("30001", limit=None, trusted_only=True)
    }
    passed = {level for level, memory_id in ids.items() if memory_id in recalled}
    # 先断言集合层面（漂移时一眼看出是哪一档），再逐条断言失败定位
    assert passed == set(TRUSTED_TRUST_LEVELS)
    for level, memory_id in ids.items():
        assert (memory_id in recalled) is (level in TRUSTED_TRUST_LEVELS), level


def test_profile_gate_only_passes_trusted_levels(store):
    """画像注入门（active_notes）与召回门必须是同一个集合——两道门不能分家。"""
    memory = MemorySystem(store=store)
    qq_by_level = {}
    for index, level in enumerate(LEVELS):
        qq = f"3050{index}"
        qq_by_level[level] = qq
        store.get_or_create_person(qq, "测试人")
        store.update_person(qq, actor="test", notes=f"画{index}",
                            notes_trust_level=level)
    for level, qq in qq_by_level.items():
        assert bool(memory.active_notes(qq)) is (level in TRUSTED_TRUST_LEVELS), level


def test_capabilities_agree_with_recall_gate(store):
    """「可注入」必须等于真实召回放行条数——这是这一期唯一要保证的事。"""
    for index, level in enumerate(LEVELS):
        _memory_with_trust(store, "30201", level, f"口径事实{index}")
    caps = store.memory_capabilities()
    recalled = store.query_memories("", limit=None, trusted_only=True)
    assert caps["trusted_injectable"] == len(recalled)
    assert caps["trusted_blocked"] == caps["memories_active"] - caps["trusted_injectable"]
    assert caps["trusted_cover"] == (
        round(caps["trusted_injectable"] / caps["memories_active"], 4)
        if caps["memories_active"] else 0.0
    )
    assert caps["memories_with_evidence"] <= caps["memories_active"]
    assert 0.0 <= caps["evidence_cover"] <= 1.0


def test_capabilities_on_fresh_store_do_not_crash(store):
    """全新库也要能算出比例（剖面页在全新部署时要打得开，不能零除）。"""
    caps = store.memory_capabilities()
    assert isinstance(caps["trusted_cover"], float)
    assert isinstance(caps["evidence_cover"], float)
    assert caps["trusted_blocked"] >= 0
    assert caps["people_profiled_blocked"] >= 0
    if caps["memories_active"] == 0:
        assert caps["trusted_cover"] == 0.0
        assert caps["evidence_cover"] == 0.0


def test_dirty_profile_is_counted_as_blocked(store):
    """dirty 画像不注入（active_notes 先看 notes_dirty），所以它必须算在「被挡」一侧。

    如果统计只按 trust_level 判断，一份 verified 但 dirty 的画像会被算成「可注入」，
    而实际注入是空的——那正是「界面说能注入、实际注入不了」的假绿。
    """
    before = store.memory_capabilities()
    store.get_or_create_person("30601", "甲")
    store.update_person("30601", actor="test", notes="一份还没重合成完的画像",
                        notes_trust_level="verified", notes_dirty=1)
    memory = MemorySystem(store=store)
    after = store.memory_capabilities()

    assert memory.active_notes("30601") == ""          # 闸门：不注入
    assert after["people_profiled"] == before["people_profiled"] + 1
    assert after["people_profiled_injectable"] == before["people_profiled_injectable"]
    assert after["people_profiled_blocked"] == before["people_profiled_blocked"] + 1


def test_person_counts_expose_trust_gate(store):
    """单人剖面（剖面页直接用这一份）也要能回答「记得多少 / 用得上多少」。

    这里用 `__new__` 绕过 ConsoleAPI.__init__：构造真对象会 new 一个 ConsoleStore，
    那会碰状态目录，单测不该有这种副作用。这也是为什么只测这一个纯读方法。
    """
    api = ConsoleAPI.__new__(ConsoleAPI)
    api.handler = SimpleNamespace(
        memory=SimpleNamespace(store=store), owner_qq="", bot_qq="",
        personality=None, config={},
    )
    for index, level in enumerate(LEVELS):
        _memory_with_trust(store, "30301", level, f"单人事实{index}")

    counts = api.memory_profile("30301")["counts"]
    assert counts["active"] == len(LEVELS)
    assert counts["trusted_injectable"] == len(TRUSTED_TRUST_LEVELS)
    assert counts["trusted_blocked"] == len(LEVELS) - len(TRUSTED_TRUST_LEVELS)
