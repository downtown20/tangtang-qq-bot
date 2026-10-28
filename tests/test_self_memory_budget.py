"""自忆块的体积闸门（2026-10-25 第十轮）。

## 为什么需要这个文件

`_get_self_memory_context` 检索 10 条 → 格式化 5 条 → 背景预算按
`MAX_BACKGROUND_BLOCK_TOKENS` / 剩余预算裁剪，而 `truncate_bg_block` **按行切**。
所以「块的体积」直接等于「她这一轮能看见几条自忆」。

第十轮实测（用她真答出来的 5 条真数据，`memories#21375/#21416/#20954/#21148/#21117`）：

| 指标 | 旧格式 | 修后 |
|---|---|---|
| 单条 | 40~51t | 14~25t |
| 5 条合计 | 242t | 116t |
| 150t 预算下保留 | **2 条** | **5 条** |
| 240t（单块上限）下保留 | **3 条** | **5 条** |

旧格式的前缀是
`[承诺过 | 2026-08-27 22:22 | 置信度0.70 | 来源 chat_log#236563 私聊] `，
50 个字符 ≈33t，而 promise 类自忆的正文只有 9~20 字（≈6~14t）——
**68% 的预算花在元数据上**。其中 `置信度` 在 1673 条 active 自忆里全是 0.70
（写入侧硬编码），`chat_log#N` 她没有任何工具能按 id 取回。

## 这个文件防什么

以前体积回退**看不见**：日志写「自忆 249→150t」，那个数字不传达「5 条变 2 条」。
这里把「条数」变成断言——体积一旦涨回去，掉的是**条数**，测试立刻红。
"""

from agent.context_builder import estimate_tokens
from agent.memory import MemorySystem
from agent.protocols import truncate_bg_block

# 五条真实形态的 promise 自忆（短正文，正是最吃亏的那一类）
FIVE = [
    {"id": 20954, "key": "promise", "value": "承诺把「啥子！！！」补传过去，传完报告",
     "timestamp": "2026-08-27 22:22:00", "confidence": 0.7,
     "evidence_ids": "236563", "source_group_id": ""},
    {"id": 21117, "key": "promise", "value": "承诺三分钟后喊你。",
     "timestamp": "2026-08-29 01:32:00", "confidence": 0.7,
     "evidence_ids": "241110", "source_group_id": ""},
    {"id": 21148, "key": "promise", "value": "承诺会认真捧场，该笑该夸一个不落",
     "timestamp": "2026-08-29 19:33:00", "confidence": 0.7,
     "evidence_ids": "242038", "source_group_id": ""},
    {"id": 21375, "key": "promise", "value": "明天白天你随便提，我说了算数",
     "timestamp": "2026-09-22 03:40:00", "confidence": 0.7,
     "evidence_ids": "250659", "source_group_id": ""},
    {"id": 21416, "key": "promise", "value": "回头给标题的话，我会认真查了再答你，不让空着手瞎猜。",
     "timestamp": "2026-09-22 16:09:00", "confidence": 0.7,
     "evidence_ids": "250978", "source_group_id": ""},
]


def _block(rows):
    return MemorySystem._format_self_memories(None, rows)


def test_five_self_memories_survive_150t_truncation():
    """150t 预算下 5 条必须全部保留——旧格式这里只留 2 条。

    150t 对应生产日志里真实出现过的裁剪结果（「自忆 ...→150t」）。
    """
    text = _block(FIVE)
    cut = truncate_bg_block(text, 150)
    assert cut.count("💭") == 5, (
        f"150t 下只保留了 {cut.count('💭')}/5 条自忆（体积回退了？）\n{cut}")


def test_five_self_memories_survive_block_cap():
    """单块上限 240t（MAX_BACKGROUND_BLOCK_TOKENS）下也是 5 条。

    旧格式 5 条 242t > 240，会因单块上限被砍掉第 5 条——即使预算充足。
    """
    text = _block(FIVE)
    cut = truncate_bg_block(text, 240)
    assert cut.count("💭") == 5
    assert estimate_tokens(text) <= 240


def test_single_self_memory_line_is_compact():
    """单条自忆 ≤ 25t——旧格式是 40~51t。

    上限松到 25t 是为了容纳长正文（`v[:80]`），但它必须远低于旧前缀的成本。
    """
    for m in FIVE:
        one = _block([m]).split("\n")[1]
        assert estimate_tokens(one) <= 25, f"单条 {estimate_tokens(one)}t 超标: {one}"


def test_noise_fields_do_not_come_back():
    """`置信度` 与 `chat_log#` 不得回到注入文本里。

    前者在 1673 条 active 自忆里是同一个值（零信息）；后者她没有工具能按
    id 取回（第 9 轮「假取回路径」的教训：名字像不等于能取回）。
    """
    text = _block(FIVE)
    assert "置信度" not in text
    assert "chat_log#" not in text
    # provenance 声明与可区分字段必须还在
    assert "有原始消息支持" in text
    assert "承诺过" in text
    assert "08-27" in text


def test_group_scope_still_visible_and_empty_timestamp_safe():
    """群聊作用域仍然逐条可见；时间戳缺失时不产生 `[5:10]` 切片垃圾。"""
    row = dict(FIVE[0], source_group_id="30001", timestamp="")
    text = _block([row])
    assert "群30001" in text
    assert "日期未知" in text
    assert text.count("\n") == 1
