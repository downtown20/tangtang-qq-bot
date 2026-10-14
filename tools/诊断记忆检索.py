"""
记忆检索诊断脚本 —— 模拟糖糖收到消息时的全链路检索过程
显示：BGE粗排→Reranker精排→最终注入prompt的记忆文本

用法:
  python tools/诊断记忆检索.py <qq_id> <消息文本>
  python tools/诊断记忆检索.py 2524950083 "好啦，不用解释这么多"

如果不带参数，自动测试主人(2524950083)的多条典型消息。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.store import Store
from agent.memory import MemorySystem

def diagnose(qq_id: str, message: str, label: str = "", group_id: str | None = None):
    store = Store("memory.db")
    memory = MemorySystem(db_path="memory.db", store=store)

    print(f"\n{'='*60}")
    print(f"🧪 {label or '诊断'}")
    print(f"   QQ: {qq_id}")
    print(f"   消息: {message[:100]}")
    print(f"{'='*60}")

    # ── Step 1: people 表 ──
    person = memory.get_or_create_person(qq_id)
    print(f"\n📋 people表:")
    print(f"   昵称: {person.get('nickname', '?')}")
    print(f"   聊天次数: {person.get('total_chats', 0)}")
    print(f"   亲密度: {person.get('intimacy', 0)}")
    notes = person.get('notes', '')
    print(f"   notes({len(notes)}字): {notes[:200] if notes else '(空)'}")
    print(f"   初识: {person.get('first_met', '?')}")

    # ── Step 2: recall() 全量记忆 ──
    all_mems = memory.recall(qq_id, limit=50, query_text="")  # 不限语义，全量
    print(f"\n🧠 全量记忆({len(all_mems)}条):")
    for m in all_mems:
        imp = getattr(m, 'importance', 0)
        eff = getattr(m, 'effective', 0)
        conf = getattr(m, 'confidence', 0)
        cog = getattr(m, 'cognitive', '?')
        key = getattr(m, 'key', '?')
        val = getattr(m, 'value', '')
        ts = getattr(m, 'timestamp', '')[:10]
        rec = getattr(m, 'recall_count', 0)
        print(f"  [{ts}] {key}/{cog} imp={imp} eff={eff:.1f} conf={conf:.1f} rec={rec}")
        print(f"    → {val[:120]}")

    # ── Step 3: recall_formatted() → 实际注入prompt的文本 ──
    print(f"\n📝 recall_formatted() 注入文本:")
    formatted = memory.recall_formatted(qq_id, limit=25)
    print("---BEGIN---")
    print(formatted)
    print("---END---")

    # ── Step 4: format_compact_memories() → 精简版 ──
    print(f"\n📝 format_compact_memories() 精简版:")
    compact = memory.format_compact_memories(qq_id, all_mems[:8], max_facts=6)
    print("---BEGIN---")
    print(compact)
    print("---END---")

    # ── Step 5: 自忆注入检查 ──
    # 自忆不是全局人格池：必须绑定当前对话对象（以及可选群作用域）。
    # 旧脚本省略 target_qq，打印的是 bot 全局所有自忆，容易把诊断结果
    # 误读成“每个用户都会收到同一组承诺”。
    bot_qq = "2693914170"
    self_mems = memory.recall(
        bot_qq, limit=10, query_text="", target_qq=str(qq_id),
        grounded_only=True, source_group_id=group_id,
    )
    scope = f"群{group_id}" if group_id else "当前对象（全部已绑定作用域）"
    print(f"\n🤖 糖糖自忆({len(self_mems)}条，作用域={scope}):")
    for m in self_mems[:8]:
        key = getattr(m, 'key', '?')
        val = getattr(m, 'value', '')
        imp = getattr(m, 'importance', 0)
        print(f"  [{key}] imp={imp}: {val[:100]}")


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        # 手动指定
        diagnose(sys.argv[1], sys.argv[2], group_id=sys.argv[3] if len(sys.argv) >= 4 else None)
    else:
        # 自动测试
        test_cases = [
            ("2524950083", "好啦，不用解释这么多，现在是凭你的本能记忆记住的我们之间的聊天对吗？", "主人-记忆质疑"),
            ("2524950083", "帮我查一下天气", "主人-功能请求"),
            ("2524950083", "你还记得我上次说的那个bug吗", "主人-回忆请求"),
            ("2872952283", "我考试考砸了", "486-情绪表达"),
            ("2377255095", "今天想吃什么", "群主-日常对话"),
        ]
        for qq, msg, label in test_cases:
            diagnose(qq, msg, label)
