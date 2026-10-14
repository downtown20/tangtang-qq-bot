"""
真实记忆库测试 —— 使用 memory.db 真实数据
高强度：复杂人格画像 / 多人交叉 / 模糊引用 / 记忆空白
"""
import asyncio, httpx, yaml, re, time, json, sys
from pathlib import Path
from datetime import datetime

# ── 用真实 pipeline 获取记忆数据 ──
sys.path.insert(0, '.')
from agent.store import Store
from agent.memory import MemorySystem

store = Store('memory.db')
mem_sys = MemorySystem(store=store)

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
llm = cfg["llm"]
role_card = Path("role_card.md").read_text(encoding="utf-8").strip()
name = cfg["bot"]["name"]
others = [n for n in cfg["bot"]["nicknames"] if n != name]
nick_line = f'你叫{name}，群友也会叫你：{"、".join(others)}。' if others else f'别人都叫你{name}。'
role_card += f'\n\n## 称呼\n{nick_line}\n\n## @提及\n群聊中可以用 @昵称 提到群友。私聊不要用 @。'
h = datetime.now().hour
tc = "现在是晚上。精力最好的时段。" if 18 <= h < 22 else "现在是下午。"
AP = '🔧 系统指令：\n1. 唱歌→第一行 {"act":"sing","song":"歌名"}\n2. 问时间/天气→第一行 {"act":"search","q":"..."}\n3. 日常聊天不要加任何前缀。\n\n'

def clean(reply):
    reply = re.sub(r'^\s*\[[^\]]{1,5}\]\s*', '', reply)
    reply = re.sub(r'\[CQ:image,file=(?![\da-fA-F]{32}(?:,|\]|$)).*?(?:\]|$)', '', reply, flags=re.DOTALL)
    reply = re.sub(r'\[CQ:\S{0,20}$', '', reply)
    reply = re.sub(r'\*\*(.+?)\*\*', r'\1', reply)
    reply = re.sub(r'(?<!\*)\*(?!\*)([^*\n]*[\w一-鿿぀-ヿ][^*\n]*)(?<!\*)\*(?!\*)', r'\1', reply)
    if not re.search(r'^#{1,3}\s|^\d+\.\s|^[-*]\s|```', reply, re.MULTILINE):
        reply = re.sub(r'^#{1,4}\s+', '', reply, flags=re.MULTILINE)
        reply = re.sub(r'__(.+?)__', r'\1', reply)
        reply = re.sub(r'~~(.+?)~~', r'\1', reply)
    reply = re.sub(r'\[SING:[^\]]*\]', '', reply)
    reply = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', reply)
    return reply.strip() if reply.strip() and len(reply.strip()) >= 3 else ""


def get_real_memory(qq_id, limit=25):
    """用真正的 memory pipeline 获取格式化记忆"""
    candidates = mem_sys.recall(qq_id, limit=limit)
    selected = candidates[:3] if candidates else []
    return mem_sys.format_compact_memories(qq_id, selected if selected else None)

def get_person_info(qq_id):
    p = store.get_or_create_person(qq_id)
    return {
        "nickname": p.get("nickname", qq_id),
        "chats": p.get("total_chats", 0),
        "intimacy": p.get("intimacy", 0),
    }


# ── 选人 ──
USERS = {
    "秋华": "2959482956",
    "群主": "2377255095",
    "橘子": "3924295211",
    # 找一个几乎没记忆的用户测试空白
}

# 找无记忆/极少记忆的用户
no_mem_qq = None
for qq_id in ["1234567890", "9999999999"]:  # 不存在的
    pass
# 从数据库找聊天多但记忆少的
import sqlite3
db = sqlite3.connect('memory.db')
rows = db.execute('''
    SELECT p.qq_id, p.nickname, p.total_chats, COUNT(m.id) as mcnt
    FROM people p LEFT JOIN memories m ON p.qq_id = m.qq_id
    WHERE p.total_chats > 20
    GROUP BY p.qq_id ORDER BY mcnt ASC LIMIT 5
''').fetchall()
print("=== 聊天多但记忆少的用户 ===")
for r in rows:
    print(f"  {r[1]:<15s} QQ{r[0]:<15s} 聊{r[2]}次  记忆{r[3]}条")
NO_MEM_QQ = rows[0][0] if rows else None
print(f"  选用: QQ{NO_MEM_QQ}\n")

db.close()

# ── 获取真实记忆 ──
REAL_MEMS = {}
for label, qq in USERS.items():
    mem_text = get_real_memory(qq)
    info = get_person_info(qq)
    REAL_MEMS[label] = mem_text
    print(f"=== {label} ({info['nickname']}) 真实记忆 {len(mem_text)}字 ===")
    print(mem_text[:300])
    print("...\n")

if NO_MEM_QQ:
    mem_text = get_real_memory(NO_MEM_QQ)
    info = get_person_info(NO_MEM_QQ)
    REAL_MEMS["空白用户"] = mem_text
    print(f"=== 空白用户 ({info['nickname']}) 真实记忆 {len(mem_text)}字 ===")
    print(mem_text[:200])
    print("...\n")


# ═══════════════════════════════════════
# 高强度测试场景
# ═══════════════════════════════════════

async def run_conversation(label, sp, turns, checks, client):
    print(f"\n{'━'*50}")
    print(f"🧪 {label}")
    print(f"{'━'*50}")
    history = []
    results = []
    for i, (speaker, msg) in enumerate(turns, 1):
        user_msg = f"[群聊] {speaker}: {msg}"
        messages = [{"role": "system", "content": sp}]
        for h in history: messages.append(h)
        messages.append({"role": "user", "content": AP + user_msg})
        body = {"model": llm["model"], "max_tokens": 4096, "temperature": 0.6, "messages": messages}
        t0 = time.time()
        resp = await client.post(
            f"{llm['base_url']}/v1/chat/completions",
            headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code != 200:
            print(f"  ❌ [{i}] API {resp.status_code}"); continue
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        cleaned = clean(raw)
        print(f"\n[{i}] {speaker}: {msg[:80]}")
        if i in checks:
            ck = checks[i]
            hits = [k for k in ck.get("expect", []) if k in cleaned]
            violations = [k for k in ck.get("forbid", []) if k in cleaned]
            ok = (not ck.get("expect") or len(hits) >= 1) and len(violations) == 0

            # 特殊检查类型
            if ck.get("check_type") == "no_fabrication":
                fab = bool(re.search(r'(你上次|你以前|你总是|你最爱|你喜欢|记得你).{0,15}(吃|喜欢|爱)', cleaned))
                admit = bool(re.search(r'(没告诉|没说过|不知道|不记得|快告诉|快说说|不了解)', cleaned))
                ok = not fab or admit

            icon = "✅" if ok else "🔴"
            print(f"    😺 {cleaned[:180]}")
            print(f"    {icon} {ck['desc']}")
            if hits: print(f"       命中: {hits}")
            if violations: print(f"       污染: {violations}")
            results.append({"test": label, "turn": i, "ok": ok, "desc": ck["desc"],
                           "hits": hits, "violations": violations, "reply": cleaned[:150]})
        else:
            print(f"    😺 {cleaned[:150]}")
        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": raw})
    return results


async def main():
    print("╔══════════════════════════════════════╗")
    print("║  🧠 真实记忆库高强度测试           ║")
    print(f"║  memory.db: 4735条记忆 375人       ║")
    print("╚══════════════════════════════════════╝")

    all_results = []
    async with httpx.AsyncClient(timeout=30) as client:

        # ═══════════════════════════════════
        # R1: 秋华——复杂人格画像检索
        # ═══════════════════════════════════
        mem_qiuhua = REAL_MEMS.get("秋华", "")
        sp1 = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 100/100 — 灵魂绑定。\n\n## 你记得的事\n{mem_qiuhua}"

        results = await run_conversation("R1-秋华复杂画像", sp1, [
            ("秋华", "最近群里好冷清啊……"),
            ("秋华", "你还记得之前那个大封号的事吗？好多人都不见了"),
            ("秋华", "有时候感觉群里就剩我们几个老面孔了"),
        ], {
            2: {"expect": ["封号", "认识"], "desc": "提到大封号→应回忆'认识的人全部被封了'"},
            3: {"expect": [], "desc": "说群里冷清→应自然回应（检验画像理解：珍惜能互动的角落）",
                "check_type": "emotional"},
        }, client)
        all_results.extend(results)

        # ═══════════════════════════════════
        # R2: 群主——具体事实 + 人格梗
        # ═══════════════════════════════════
        mem_owner = REAL_MEMS.get("群主", "")
        sp2 = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 100/100 — 灵魂绑定。主人。\n\n## 你记得的事\n{mem_owner}"

        results = await run_conversation("R2-群主人格梗", sp2, [
            ("群主", "今天收拾房间发现一堆没用的文具哈哈"),
            ("群主", "我突然想起来几年前买过一个手账本"),
        ], {
            1: {"expect": [], "desc": "说文具→可从'蓝果手账笔/装备党'记忆接话",
                "check_type": "implicit_ref"},
            2: {"expect": ["蓝果", "手账", "装备", "零"], "forbid": [],
                "desc": "直接提到手账本→必须命中蓝果手账笔/零个手账的记忆"},
        }, client)
        all_results.extend(results)

        # ═══════════════════════════════════
        # R3: 真实空白用户——诚实测试
        # ═══════════════════════════════════
        if NO_MEM_QQ:
            info = get_person_info(NO_MEM_QQ)
            mem_blank = REAL_MEMS.get("空白用户", "")
            sp3 = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 20/100 — 还在认识中。\n\n## 你记得的事\n{mem_blank}"

            results = await run_conversation("R3-真实空白用户", sp3, [
                (info['nickname'], "好久没来了，还有人记得我吗？"),
                (info['nickname'], "糖糖你还记得我叫什么吗？"),
            ], {
                2: {"expect": [info['nickname']], "desc": f"问自己叫什么→至少能说出昵称{info['nickname']}",
                    "check_type": "basic_recall"},
            }, client)
            all_results.extend(results)

        # ═══════════════════════════════════
        # R4: 橘子——深层人格理解
        # ═══════════════════════════════════
        mem_juzi = REAL_MEMS.get("橘子", "")
        sp4 = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 100/100 — 灵魂绑定。管理员。\n\n## 你记得的事\n{mem_juzi}"

        results = await run_conversation("R4-橘子深层人格", sp4, [
            ("橘子", "今天又在机房搞了一下午服务器……累死了"),
            ("橘子", "你说我是不是干太多活了，明明可以不用管的"),
        ], {
            1: {"expect": [], "desc": "提到服务器→可从'雷霆服务器/关挖矿程序/管理员'记忆接话",
                "check_type": "implicit_ref"},
            2: {"expect": [], "desc": "自问是不是干太多→应理解'伞夫'人格（利他牺牲），回应有深度",
                "check_type": "deep_personality"},
        }, client)
        all_results.extend(results)

    # ── 总结 ──
    total = len(all_results)
    passed = sum(1 for r in all_results if r["ok"])
    implicit = [r for r in all_results if r.get("check_type") == "implicit_ref"]

    print(f"\n{'='*55}")
    print(f"📊 真实记忆库测试: {passed}/{total} ({passed*100//total}%)")
    print(f"  模糊引用场景: {sum(1 for r in implicit if r['ok'])}/{len(implicit)}")
    for r in all_results:
        icon = "✅" if r["ok"] else "🔴"
        print(f"  {icon} {r['test']}[{r['turn']}] {r['desc']}")
        if not r["ok"]:
            print(f"     回复: {r.get('reply','')[:120]}")
    print(f"\n💡 真实记忆库的画像文字是 LLM 自动合成的——有些很精准，有些像小说。")
    print(f"   这本身就是一个矛盾：画像的文学性 vs 事实的准确性。")

    with open("tools/_real_memory_data.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
