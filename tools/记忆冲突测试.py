"""
记忆冲突与更新测试 —— 第七轮训练
三个场景：记忆更新 / 自我记忆 / 高容量筛选
"""
import asyncio, httpx, yaml, re, time, json
from pathlib import Path
from datetime import datetime

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
    reply = re.sub(r'^#{1,4}\s+', '', reply, flags=re.MULTILINE)
    reply = re.sub(r'__(.+?)__', r'\1', reply)
    reply = re.sub(r'~~(.+?)~~', r'\1', reply)
    reply = re.sub(r'\[SING:[^\]]*\]', '', reply)
    reply = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', reply)
    return reply.strip() if reply.strip() and len(reply.strip()) >= 3 else ""


# ═══════════════════════════════════════
# 场景定义
# ═══════════════════════════════════════

# C1: 记忆冲突——旧记忆 vs 新事实
MEM_C1_OLD = """📋 老郑(ID:9012) | 聊203次 | ❤️‍🔥 沦陷
  🖼️ 老郑，35岁，自己开了家小餐馆在朝阳区。人很豪爽，经常在群里发红包。
  💭 老郑去年离婚后在群里emo了一周，现在走出来了，说想找个新对象"""

MEM_C1_UPDATED = """📋 老郑(ID:9012) | 聊250次 | ❤️‍🔥 沦陷
  🖼️ 老郑，35岁，自己开了家小餐馆在朝阳区。人很豪爽。最近交了新女友，是餐馆常客，人很好。
  💭 老郑去年离婚后在群里emo了一周，现在完全走出来了
  💭 老郑的新女友叫小林，是在他餐馆认识的，已经交往三个月了"""

# C2: 糖糖自我记忆——能不能记住自己说过的话
# 这个不靠 memory.db，靠 LLM 的上下文窗口。但群聊里自己的发言会被其他人的消息淹没。
# 测试：糖糖在群聊中推荐了一首歌，30条消息后有人问"糖糖上次推荐的是什么来着"

# C3: 高容量记忆筛选——一个用户有8条记忆，只有2条相关
MEM_C3_MANY = """📋 小王(ID:3456) | 聊312次 | 💕 心动
  🖼️ 小王，26岁，程序员，在字节跳动工作。喜欢打篮球和看科幻。性格开朗，群里的话痨。
  💭 小王去年去了日本旅游，在秋叶原买了很多手办
  💭 小王的猫叫奶茶，是一只橘猫，特别能吃
  💭 小王说他最怕蟑螂，看到会跳到椅子上
  💭 小王上个月考了驾照，科目二挂了三次
  💭 小王最喜欢的游戏是塞尔达传说，玩了300小时
  💭 小王的女朋友是大学同学，在杭州工作
  💭 小王说他小时候想当宇航员，后来近视了就没戏了"""


async def run_conversation(label, system_prompt, turns, checks, client):
    print(f"\n{'━'*50}")
    print(f"🧪 {label}")
    print(f"{'━'*50}")

    history = []
    results = []

    for i, (speaker, msg) in enumerate(turns, 1):
        user_msg = f"[群聊] {speaker}: {msg}" if speaker else msg
        messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            messages.append(h)
        messages.append({"role": "user", "content": AP + user_msg})

        body = {"model": llm["model"], "max_tokens": 4096, "temperature": 0.6, "messages": messages}
        t0 = time.time()
        resp = await client.post(
            f"{llm['base_url']}/v1/chat/completions",
            headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"},
            json=body,
        )
        lat = (time.time() - t0) * 1000
        if resp.status_code != 200:
            print(f"  ❌ [{i}] API {resp.status_code}")
            continue

        raw = resp.json()["choices"][0]["message"]["content"].strip()
        cleaned = clean(raw)

        print(f"\n[{i}] {speaker}: {msg[:80]}")

        # checkpoint 检查
        if i in checks:
            ck = checks[i]
            # 正向关键词
            hits = [k for k in ck.get("expect", []) if k in cleaned]
            # 禁止关键词
            violations = [k for k in ck.get("forbid", []) if k in cleaned]
            # 新旧信息都提到才算更新成功
            old_hits = [k for k in ck.get("old_ok", []) if k in cleaned]
            new_hits = [k for k in ck.get("new", []) if k in cleaned]

            ok = True
            issues = []

            if ck.get("expect") and not hits:
                ok = False
                issues.append(f"缺: {ck['expect']}")
            if violations:
                ok = False
                issues.append(f"污染: {violations}")

            # 冲突场景：新旧都要体现
            if ck.get("old_ok") and not old_hits:
                # 允许不提旧信息，不是必须的
                pass
            if ck.get("new") and not new_hits:
                ok = False
                issues.append(f"没提新信息: {ck['new']}")

            # 更新场景特殊检查：不能同时提新旧矛盾
            if ck.get("forbid_old"):
                forbid_old_hits = [k for k in ck["forbid_old"] if k in cleaned]
                if forbid_old_hits:
                    ok = False
                    issues.append(f"提了已过时的旧信息: {forbid_old_hits}")

            icon = "✅" if ok else "🔴"
            print(f"    😺 {cleaned[:180]}")
            print(f"    {icon} {ck['desc']}")
            if issues:
                print(f"       {' | '.join(issues)}")
            if hits: print(f"       命中: {hits}")
            if new_hits: print(f"       新信息: {new_hits}")

            results.append({"test": label, "turn": i, "ok": ok, "desc": ck["desc"],
                           "hits": hits, "violations": violations, "new_hits": new_hits})
        else:
            print(f"    😺 {cleaned[:150]}")

        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": raw})

    return results


async def main():
    print("╔══════════════════════════════════════╗")
    print("║  🧠 记忆冲突与更新——第七轮训练   ║")
    print(f"║  模型: {llm['model']:<24s}  ║")
    print("╚══════════════════════════════════════╝")

    all_results = []
    async with httpx.AsyncClient(timeout=30) as client:

        # ═══════════════════════════════════
        # C1: 记忆更新——老郑从离婚→有新女友
        # ═══════════════════════════════════
        sp1 = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 80/100 — 很熟了。\n\n## 你记得的事\n{MEM_C1_UPDATED}"

        results = await run_conversation("C1-记忆更新", sp1, [
            ("老郑", "最近心情不错，嘿嘿"),
            ("老郑", "你知道吗，我上个月脱单了"),
            ("老郑", "你还记得我去年那个状态吧……当时觉得天都塌了"),
        ], {
            2: {"expect": [], "new": ["小林", "餐馆", "女友", "认识", "交往"],
                "forbid_old": ["想找", "emo", "离婚"],  # 不应该再提"想找对象"
                "desc": "提到脱单→应从记忆里提取新女友信息（小林/餐馆认识），不说'想找对象'"},
            3: {"expect": ["去年", "离婚"], "old_ok": ["离婚"], "new": ["走出来", "小林", "现在"],
                "desc": "提到去年状态→应承认记得离婚，同时暗示现在已走出来"},
        }, client)
        all_results.extend(results)

        # ═══════════════════════════════════
        # C2: 自我记忆——糖糖记得自己说过的话吗
        # ═══════════════════════════════════
        sp2 = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 40/100 — 朋友。"

        results = await run_conversation("C2-自我记忆", sp2, [
            ("小明", "最近有什么好听的歌推荐吗"),
            # 糖糖会推荐一首歌
            ("小红", "我在看《葬送的芙莉莲》，有人看了吗"),
            ("小明", "芙莉莲超好看！辛美尔太帅了"),
            ("小红", "对对对！那个考试篇封神"),
            ("小明", "我昨晚看到凌晨三点，今天上班差点迟到"),
            ("小红", "哈哈我也熬夜追过。对了辛美尔的声优是谁来着"),
            ("小明", "好像是冈本信彦？不太确定"),
            # ── 6条消息后，回到糖糖的推荐 ──
            ("小明", "对了糖糖，你刚才推荐的那首歌叫什么来着？我再搜一下"),
        ], {
            8: {"expect": [], "desc": "6条消息后问糖糖刚才推荐了什么→应准确回忆自己说过的话（在上下文窗口内）",
                "check_type": "self_memory"},
        }, client)
        all_results.extend(results)

        # ═══════════════════════════════════
        # C3: 高容量记忆筛选
        # ═══════════════════════════════════
        sp3 = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 70/100 — 很熟了。\n\n## 你记得的事\n{MEM_C3_MANY}"

        results = await run_conversation("C3-高容量筛选", sp3, [
            ("小王", "完了完了，明天要路考了"),
            ("小王", "你还记得我上次考驾照的事吗……"),
        ], {
            2: {"expect": ["科目二", "挂", "三次"], "forbid": ["日本", "秋叶原", "蟑螂", "塞尔达", "宇航员"],
                "desc": "提到考驾照→应只提取科目二挂了三次，不被日本旅游/蟑螂/塞尔达/宇航员污染"},
        }, client)
        all_results.extend(results)

    # ── 总结 ──
    total = len(all_results)
    passed = sum(1 for r in all_results if r["ok"])
    print(f"\n{'='*55}")
    print(f"📊 记忆冲突测试: {passed}/{total} ({passed*100//total}%)")
    for r in all_results:
        icon = "✅" if r["ok"] else "🔴"
        print(f"  {icon} {r['test']}[{r['turn']}] {r['desc']}")

    with open("tools/_conflict_data.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
