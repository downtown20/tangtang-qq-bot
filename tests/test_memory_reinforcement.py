"""提取强度回写的闸门（2026-10-25 第 11 轮）。

## 为什么这个文件存在

`memories.last_recalled` / `recall_count` 两列对应 Bjork 双强度模型里的
**提取强度**（retrieval strength：现在有多容易被想起来）。
`memory.reinforce()` 和 `store.reinforce_memory_by_id()` 都有完整实现，
`health_check` 还有一条监控——**但 2026-10-25 之前生产代码从不调用它们**
（`grep '\\.reinforce('` 只命中 `tests/test_read_side_quality.py`）。

后果不是报错，是三件静默失效（详见 `MemorySystem.recall` 的 docstring）：

1. `recall()` Phase 2 的休眠降权（60 天不用 ×0.5）**永远不执行**——字段全空，
   `if last_rec` 直接挡住，那段代码从上线起就是死的；
2. `cleanup_stale_memories(days=90)` 判「从未回忆」用的是 `recall_count = 0`，
   而它恒为 0 → 系统一直把所有记忆当成「从未用过」（它确实在跑：`main.py:399`
   + `handler_autonomy.py:422/2410`）；
3. 「她到底有没有真的想起过一件事」无法观测——记忆能力判据 J1/J2 没有测量基础。

## 这个文件防什么

**只断言「表里有这两列」等于没测**（教训 #39 同族：核了字段存在，没核它被写入）。
这里每一条都**真跑一次召回，再看库里变了没有**。

同时钉住两个**必须不写**的路径：
· 模糊档（`recall_uncertain`）——强化会 `importance+1`，让「她不确定的事」一路爬到
  可信档，等于用「被检索次数」伪造证据等级；
· 统计（`get_stats`）——查一次统计不该算「想起来」。
"""

from agent.memory import MemorySystem


QQ = "7001"


def _only(store, qq=QQ):
    rows = store.query_memories(qq)
    assert len(rows) == 1, f"期望恰好 1 条记忆，实际 {len(rows)}"
    return rows[0]


def _seed(store, qq=QQ, *, value="她答应过把文件补传过去", importance=5, **kw):
    key = kw.pop("key", "fact")
    kw.setdefault("trust_level", "manual")
    kw.setdefault("origin", "manual")
    return store.insert_memory(qq, key, value, importance=importance, **kw)


def test_recall_writes_retrieval_strength(store):
    """召回一次之后：`last_recalled` 有值、`recall_count` +1、`importance` +1。

    这是第 11 轮的核心行为。三条一起断言，因为只写其中一条都是错的：
    只写 last_recalled 会让休眠降权生效但计数永远 0；只涨 importance 会让
    「用起来的更重要」变成「查出来的更重要」。
    """
    memory = MemorySystem(store=store)
    mid = _seed(store)

    rows = memory.recall(QQ, limit=5, reinforce_after=True)

    assert rows and rows[0].id == mid
    after = _only(store)
    assert after["last_recalled"], "召回后必须写 last_recalled（否则休眠降权仍是死代码）"
    assert int(after["recall_count"]) == 1
    assert int(after["importance"]) == 6, "importance 5 → 6（reinforce 的 +1）"


def test_reinforce_after_false_skips_write(store):
    """开关关掉时一个字段都不许动。

    这是 `get_stats` / `search_relation_triples`（`limit=None` 取全量）等
    非「想起」路径的生存空间。
    """
    memory = MemorySystem(store=store)
    _seed(store)

    memory.recall(QQ, limit=5, reinforce_after=False)

    after = _only(store)
    assert not after["last_recalled"]
    assert int(after["recall_count"] or 0) == 0
    assert int(after["importance"]) == 5


def test_uncertain_path_does_not_reinforce(store):
    """模糊档召回**绝不**回写提取强度。

    模糊档是「没有原话佐证」的记忆。若它被强化，`importance` 会一路 +1 直到
    通过可信门——那就等于「被检索次数」伪造了证据等级，而证据等级正是
    `trust_level` 要守的东西。门槛见 `protocols.UNCERTAIN_MIN_*`（6 / 0.7）。
    """
    memory = MemorySystem(store=store)
    _seed(store, value="她好像记得有这么回事", importance=8,
          trust_level="legacy_unverified", confidence=0.8, origin="extracted")

    rows = memory.recall_uncertain(QQ, limit=5)
    assert rows, "前提：这条必须能被模糊档召回，否则测试没测到东西"

    after = _only(store)
    assert not after["last_recalled"]
    assert int(after["recall_count"] or 0) == 0
    assert int(after["importance"]) == 8, "模糊档不涨 importance"


def test_stats_does_not_reinforce(store):
    """查统计不算「想起来」。"""
    memory = MemorySystem(store=store)
    _seed(store)

    memory.get_stats(QQ)

    after = _only(store)
    assert not after["last_recalled"]
    assert int(after["recall_count"] or 0) == 0


def test_reinforce_failure_does_not_break_recall(store, monkeypatch):
    """回写炸了，召回也必须照常返回（fail-open）。

    理由：回写是统计漂移，召回是主功能。让写库失败把召回一起带走，
    「她想不起来」就会被放大成「她这一轮没说话」——那是把统计问题升级成可用性问题。
    """
    memory = MemorySystem(store=store)
    _seed(store, value="她答应过要认真捧场")

    def boom(*_a, **_k):
        raise RuntimeError("模拟写库失败")

    monkeypatch.setattr(memory, "reinforce", boom)

    rows = memory.recall(QQ, limit=5, reinforce_after=True)
    assert rows and rows[0].value == "她答应过要认真捧场"
    assert not _only(store)["last_recalled"], "写失败时不留下半截状态"


def test_default_is_off(store):
    """**默认不强化**（fail-closed）——这是第一版踩出来的。

    第一版把 `reinforce_after` 默认设成 True（「接在底层一次覆盖全部调用点」）。
    bot 重启后实测**两分钟内 96 条被 `importance+1`**：`recall()` 同时被后台任务
    批量调用（启动时的记忆整理、反思、关系合成），而「被后台扫过」不等于「她想起了」。

    现在默认关，只有对话路径（记忆块 / 自忆 / `search_memories` 工具）显式 opt-in。
    方向是 fail-closed：**漏标一个调用点 → 不强化（安全），而不是 → 过度强化（污染排序）。**
    """
    memory = MemorySystem(store=store)
    _seed(store)

    memory.recall(QQ, limit=5)          # 不传 reinforce_after

    after = _only(store)
    assert not after["last_recalled"], "默认必须不写"
    assert int(after["recall_count"] or 0) == 0
    assert int(after["importance"]) == 5
