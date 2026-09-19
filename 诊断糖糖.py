"""
🍬 糖糖自动诊断工具
扫描日志 + 数据库，找出糖糖运行中的问题并提出改进建议。
用法：python 诊断糖糖.py
"""

import re, sqlite3, json, os
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timedelta

BASE = Path(__file__).parent
LOG_FILE = BASE / "tangtang.log"
DB_FILE = BASE / "memory.db"


def read_logs():
    """读取最近1000行日志"""
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()[-2000:]


def analyze_logs(lines):
    """分析日志，统计各类事件"""
    stats = {
        "total": len(lines),
        "llm_calls": 0, "llm_failures": 0,
        "reply_sent": 0, "group_send_fail": 0, "private_send_fail": 0,
        "testing_blocked": 0, "whitelist_blocked": 0, "busy_skipped": 0,
        "voice_request": 0, "song_request": 0, "song_not_found": 0,
        "sticker_steal": 0, "sticker_steal_fail": 0,
        "errors": [], "warnings": [],
        "groups_seen": set(), "groups_active": set(),
        # 回复率分析
        "at_bot": 0,           # @糖糖 次数
        "name_mention": 0,     # 提到糖糖名字
        "replied_to_at": 0,    # @后回复次数
        "replied_to_name": 0,  # 提名字后回复次数
        "interjection_triggers": 0,  # 主动插话触发
        "interjection_skipped": 0,   # 主动插话被跳过
        "conversation_enders": 0,    # 终结性消息
        "interjection_on": False,    # 插话是否开启
        "interjection_thirst": 0,    # 饥渴度
        "interjection_cooldown": 0,  # 冷却时间
        "total_group_msg": 0,  # 总群消息
        "lazy_skip": 0,        # 懒回跳过
        "reply_reasons": Counter(),  # 回复原因统计
    }

    for line in lines:
        ts_match = re.search(r'^\[(\d{2}:\d{2}:\d{2})\]', line)
        ts = ts_match.group(1) if ts_match else ""

        # LLM 调用
        if "正在调用LLM生成回复" in line:
            stats["llm_calls"] += 1
        if "LLM调用失败" in line or "LLM回复失败" in line:
            stats["llm_failures"] += 1
            stats["errors"].append(("LLM失败", ts, line.strip()[-120:]))

        # 回复发送
        if "正在发送" in line or "已发送" in line or "回复内容" in line:
            stats["reply_sent"] += 1
        if "群发送失败" in line or "私信" in line and "发送失败" in line:
            if "群" in line:
                stats["group_send_fail"] += 1
            else:
                stats["private_send_fail"] += 1
            stats["errors"].append(("发送失败", ts, line.strip()[-120:]))

        # 测试模式
        if "🧪" in line and "测试模式" in line:
            stats["testing_blocked"] += 1
            stats["warnings"].append(("测试模式拦截", ts, "消息被测试模式拦截"))

        # 白名单
        if "🚷" in line and "不在白名单" in line:
            stats["whitelist_blocked"] += 1
            stats["warnings"].append(("白名单拦截", ts, line.strip()[-100:]))

        # 忙线
        if "糖糖正在忙" in line:
            stats["busy_skipped"] += 1

        # 语音/唱歌
        if "语音请求" in line or "想听语音" in line:
            stats["voice_request"] += 1
        if "唱歌" in line and "点歌" in line:
            stats["song_request"] += 1
        if "还不会唱" in line or "曲库里没有" in line:
            stats["song_not_found"] += 1
            stats["warnings"].append(("唱歌失败", ts, line.strip()[-120:]))

        # 表情包
        if "偷到表情" in line:
            stats["sticker_steal"] += 1
        if "400 Bad Request" in line and "download" in line:
            stats["sticker_steal_fail"] += 1

        # 回复率深度分析
        if "被@了" in line or "被提到了名字" in line or "决定回复" in line or "插话" in line or "决定不插话" in line or "懒得回" in line or "终结性" in line:
            # 回复原因
            if "被@了" in line:
                stats["at_bot"] += 1
                stats["reply_reasons"]["被@"] += 1
            elif "被提到了名字" in line:
                stats["name_mention"] += 1
                stats["reply_reasons"]["提名字"] += 1
            elif "决定回复" in line and "插话评分" in line:
                stats["interjection_triggers"] += 1
                stats["reply_reasons"]["主动插话"] += 1
            elif "决定不插话" in line:
                stats["interjection_skipped"] += 1
            elif "懒得回" in line:
                stats["lazy_skip"] += 1
            elif "终结性回复" in line or "话题结束" in line:
                stats["conversation_enders"] += 1
            elif "有人求评价图片" in line or "群友发图求评价" in line:
                stats["reply_reasons"]["求评价图"] += 1
            elif "有人想听语音" in line:
                stats["reply_reasons"]["语音请求"] += 1

        # 插话状态
        if "主动插话" in line and ("开" in line or "关" in line or "ON" in line or "OFF" in line):
            if "开" in line or "ON" in line:
                stats["interjection_on"] = True
            elif "关" in line or "OFF" in line:
                stats["interjection_on"] = False

        # 饥渴度/冷却
        thirst_m = re.search(r'饥渴度[：:]\s*(\d+)', line)
        if thirst_m:
            stats["interjection_thirst"] = int(thirst_m.group(1))
        cool_m = re.search(r'冷却时间[：:]\s*(\d+)', line)
        if cool_m:
            stats["interjection_cooldown"] = int(cool_m.group(1))

        # 群消息计数
        if re.search(r'\[群:\d+\]', line) and "📨" not in line:
            stats["total_group_msg"] += 1

        # 群活跃
        g_match = re.search(r'群[:：](\d+)', line)
        if g_match:
            stats["groups_seen"].add(g_match.group(1))
        g_msg = re.search(r'\[群:(\d+)\]', line)
        if g_msg:
            stats["groups_active"].add(g_msg.group(1))

        # 错误行
        if "ERROR" in line or "❌" in line or "Traceback" in line:
            stats["errors"].append(("错误", ts, line.strip()[-150:]))

    return stats


def analyze_database():
    """分析数据库健康状况"""
    if not DB_FILE.exists():
        return {"error": "数据库文件不存在"}

    conn = sqlite3.connect(str(DB_FILE))
    db = {}

    # 人物数量
    db["people_count"] = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    db["active_people"] = conn.execute(
        "SELECT COUNT(*) FROM people WHERE total_chats > 5"
    ).fetchone()[0]

    # 记忆数量
    db["memory_count"] = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    # 聊天记录
    db["chat_count"] = conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]

    # 按群分组活跃度
    groups = conn.execute(
        "SELECT group_id, COUNT(*) as cnt, MAX(timestamp) as last_ts "
        "FROM chat_log WHERE group_id != '' AND is_bot_reply = 0 "
        "GROUP BY group_id ORDER BY cnt DESC"
    ).fetchall()
    db["group_activity"] = [(g[0], g[1], g[2]) for g in groups[:10]]

    # 糖糖回复率（最近500条群消息）
    recent = conn.execute(
        "SELECT is_bot_reply FROM chat_log WHERE group_id != '' "
        "ORDER BY id DESC LIMIT 500"
    ).fetchall()
    if recent:
        total = len(recent)
        bot_replies = sum(1 for r in recent if r[0])
        db["reply_rate"] = round(bot_replies / total * 100, 1) if total else 0
    else:
        db["reply_rate"] = 0

    # 无昵称的人（可能未被识别）
    db["unnamed"] = conn.execute(
        "SELECT COUNT(*) FROM people WHERE nickname IS NULL OR nickname = '' OR nickname = '未知'"
    ).fetchone()[0]

    # 外号数量
    db["alias_count"] = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]

    # ── 内容质量深度分析 ──
    # 取最近200条消息→回复对（人类消息→紧接的糖糖回复）
    pairs = conn.execute("""
        SELECT c1.message as human_msg, c2.message as bot_reply
        FROM chat_log c1
        JOIN chat_log c2 ON c2.id = (
            SELECT MIN(id) FROM chat_log
            WHERE is_bot_reply = 1 AND group_id = c1.group_id AND id > c1.id
        )
        WHERE c1.is_bot_reply = 0 AND c1.group_id != '' AND c1.message != ''
        ORDER BY c1.id DESC LIMIT 200
    """).fetchall()

    bot_msgs = conn.execute(
        "SELECT message FROM chat_log WHERE is_bot_reply = 1 AND group_id != '' "
        "ORDER BY id DESC LIMIT 100"
    ).fetchall()

    # 工具函数：洗掉CQ码
    def clean_msg(msg):
        return re.sub(r'\[CQ:[^\]]+\]', '', msg)

    if bot_msgs and pairs:
        lengths = [len(m[0]) for m in bot_msgs]
        db["avg_reply_len"] = round(sum(lengths) / len(lengths))
        db["short_replies"] = sum(1 for l in lengths if l < 15)
        db["long_replies"] = sum(1 for l in lengths if l > 150)
        questions = sum(1 for m in bot_msgs if "？" in m[0] or "吗" in m[0] or "呢" in m[0])
        db["question_rate"] = round(questions / len(bot_msgs) * 100)

        # 开头多样性（去掉CQ码后取前6字）
        clean_openings = [clean_msg(m[0])[:6] for m in bot_msgs if len(clean_msg(m[0])) >= 6]
        openings = clean_openings
        db["opening_diversity"] = round(len(set(openings)) / max(1, len(openings)) * 100)

        # 高频开头（可能是模板化回复）
        opening_counts = Counter(clean_openings)
        db["top_openings"] = opening_counts.most_common(5)

        # 模板检测：完全相同回复
        reply_counter = Counter(m[0] for m in bot_msgs)
        db["duplicate_replies"] = sum(1 for c in reply_counter.values() if c > 1)

        # 最重复的回复（去掉CQ码后比较）
        cleaned_replies = [clean_msg(m[0]) for m in bot_msgs]
        reply_counter_clean = Counter(cleaned_replies)
        db["most_repeated"] = [(msg[:60], cnt) for msg, cnt in reply_counter_clean.most_common(3) if cnt > 1]

        # 记忆引用率：回复中提到"记得""你之前""上次"等的比例
        memory_refs = sum(1 for m in bot_msgs
                         if any(kw in m[0] for kw in ["记得", "你之前", "上次", "以前", "说过"]))
        db["memory_ref_rate"] = round(memory_refs / len(bot_msgs) * 100)

        # 关键词相关性
        overlaps = []
        for human, bot in pairs:
            h_clean = clean_msg(human)
            b_clean = clean_msg(bot)
            h_words = set(re.findall(r'[\w一-鿿]{2,}', h_clean))
            b_words = set(re.findall(r'[\w一-鿿]{2,}', b_clean))
            if h_words and len(h_clean) > 3:  # 纯图片/表情消息跳过
                overlap = len(h_words & b_words) / len(h_words)
                overlaps.append(overlap)
        db["avg_keyword_overlap"] = round(sum(overlaps) / len(overlaps) * 100) if overlaps else 0
        db["low_relevance"] = sum(1 for o in overlaps if o < 0.1)
        bot_msg_ids = [row[0] for row in conn.execute(
            "SELECT id FROM chat_log WHERE is_bot_reply = 1 AND group_id != '' ORDER BY id DESC LIMIT 100"
        ).fetchall()]
        follow_ups = 0
        for bid in bot_msg_ids:
            has_follow = conn.execute(
                "SELECT COUNT(*) FROM chat_log WHERE group_id != '' AND id > ? AND id <= ? AND is_bot_reply = 0",
                (bid, bid + 5)
            ).fetchone()[0]
            if has_follow > 0:
                follow_ups += 1
        db["follow_up_rate"] = round(follow_ups / max(1, len(bot_msg_ids)) * 100)

        # 样本：低质量回复示例
        db["low_quality_samples"] = []
        for human, bot in pairs[:50]:
            h_clean = clean_msg(human)
            b_clean = clean_msg(bot)
            h_words = set(re.findall(r'[\w一-鿿]{2,}', h_clean))
            b_words = set(re.findall(r'[\w一-鿿]{2,}', b_clean))
            if h_words and len(h_clean) > 3 and len(h_words & b_words) / len(h_words) < 0.05:
                db["low_quality_samples"].append((h_clean[:60], b_clean[:80]))
                if len(db["low_quality_samples"]) >= 3:
                    break

        db["reply_samples"] = [clean_msg(m[0])[:80] for m in bot_msgs[:5]]
    else:
        db["avg_reply_len"] = 0

    # 回复-被回复关系：谁跟糖糖互动最多
    top_responders = conn.execute(
        "SELECT qq_id, COUNT(*) as cnt FROM chat_log "
        "WHERE is_bot_reply = 1 AND group_id != '' "
        "GROUP BY qq_id ORDER BY cnt DESC LIMIT 5"
    ).fetchall()
    db["top_responders"] = [(r[0], r[1]) for r in top_responders]

    conn.close()

    # 检查 sticker metadata
    meta_file = BASE / "stickers" / "metadata.json"
    if meta_file.exists():
        db["sticker_count"] = len(json.loads(meta_file.read_text(encoding="utf-8")))
    else:
        db["sticker_count"] = 0

    return db


def diagnose():
    print("🔍 糖糖自动诊断")
    print("=" * 50)

    # ── 日志分析 ──
    lines = read_logs()
    if not lines:
        print("⚠️ 未找到日志文件，请确保 tangtang.log 存在")
        return

    s = analyze_logs(lines)
    recent_lines = [l for l in lines if _is_recent(l)]
    is_running = any("小糖糖正在苏醒" in l for l in lines[-50:])

    print(f"\n📋 日志概况：{s['total']} 行, 最近1小时: {len(recent_lines)} 行")
    if is_running:
        print("🟢 糖糖似乎正在运行")
    else:
        print("🔴 未检测到最近的启动日志，可能已停止")

    # ── 问题检测 ──
    issues = []
    suggestions = []

    # 测试模式
    if s["testing_blocked"] > 0:
        issues.append(f"⚠️ 测试模式未关闭！已拦截 {s['testing_blocked']} 条群消息")
        suggestions.append("→ 检查 config.yaml → testing_mode: false")

    # 白名单
    if s["whitelist_blocked"] > 0:
        issues.append(f"⚠️ 有群被白名单拦截 {s['whitelist_blocked']} 次")
        suggestions.append("→ 如果想让糖糖在那个群发言，加到 config.yaml 的 groups 里")

    # LLM 失败率
    if s["llm_calls"] > 0:
        fail_rate = s["llm_failures"] / s["llm_calls"] * 100
        if fail_rate > 10:
            issues.append(f"⚠️ LLM 失败率 {fail_rate:.0f}% ({s['llm_failures']}/{s['llm_calls']})")
            suggestions.append("→ 检查 API Key 和账户余额")

    # 发送失败
    if s["group_send_fail"] > 0:
        issues.append(f"⚠️ 群消息发送失败 {s['group_send_fail']} 次")
        suggestions.append("→ 可能被风控，减少发送频率试试")
    if s["private_send_fail"] > 0:
        issues.append(f"⚠️ 私信发送失败 {s['private_send_fail']} 次（可能非好友）")
        suggestions.append("→ 确保糖糖和目标用户在同一个群")

    # 忙线
    if s["busy_skipped"] > 10:
        issues.append(f"⚠️ 忙线跳过 {s['busy_skipped']} 次，群太活跃糖糖跟不上")
        suggestions.append("→ 增加插话冷却时间 /冷却 15")
    elif s["busy_skipped"] > 0:
        print(f"ℹ️ 忙线跳过 {s['busy_skipped']} 次（正常）")

    # 音乐
    if s["song_not_found"] > 0:
        issues.append(f"⚠️ {s['song_not_found']} 次点歌没找到")
        suggestions.append("→ 用 /歌单 查看曲库，检查 songs/ 文件夹")

    # 表情偷失败
    if s["sticker_steal_fail"] > 3:
        issues.append(f"⚠️ 偷表情失败 {s['sticker_steal_fail']} 次（图片链接过期）")

    # 群活跃
    print(f"\n👥 群活跃：{len(s['groups_active'])} 个群有消息")
    if s["groups_active"]:
        print(f"   群号：{'、'.join(sorted(s['groups_active']))}")

    # ── 数据库诊断 ──
    db = analyze_database()
    print(f"\n🗄 数据库：")
    print(f"   群友: {db['people_count']} 人（活跃 {db['active_people']} 人）")
    print(f"   记忆: {db['memory_count']} 条")
    print(f"   聊天: {db['chat_count']} 条")
    print(f"   外号: {db['alias_count']} 个")
    print(f"   表情包: {db['sticker_count']} 张")
    print(f"   回复率: {db.get('reply_rate', 0)}%")

    # ── 内容质量深度分析 ──
    if db.get("avg_reply_len", 0) > 0:
        print(f"\n💬 内容质量分析（最近回复）：")

        # 基础指标
        print(f"   回复长度: 平均{db['avg_reply_len']}字 | 过短{db['short_replies']}次 | 超长{db['long_replies']}次")
        print(f"   提问率: {db['question_rate']}% | 开头多样性: {db['opening_diversity']}%")
        print(f"   记忆引用率: {db.get('memory_ref_rate', 0)}% | 关键词相关性: {db.get('avg_keyword_overlap', 0)}%")
        print(f"   对话延续率: {db.get('follow_up_rate', 0)}%（糖糖回复后有人接话的比例）")

        # 模板化检测
        if db.get("most_repeated"):
            print(f"\n   🔄 模板化回复检测：")
            for msg, cnt in db["most_repeated"]:
                print(f"     出现{cnt}次: {msg}...")

        if db.get("top_openings"):
            print(f"\n   📊 高频开头（前5）：")
            for opening, cnt in db["top_openings"]:
                bar = "█" * min(cnt, 20)
                print(f"     {opening}... {cnt}次 {bar}")

        # 问题诊断
        print(f"\n   🔍 质量诊断：")
        quality_issues = []

        if db["opening_diversity"] < 30:
            quality_issues.append(("开头高度重复", "糖糖回复像机器人", "增加人格提示词里的对话变化要求"))
        if db.get("avg_keyword_overlap", 0) < 15:
            quality_issues.append(("关键词相关性极低", "糖糖可能没在回应话题，在自说自话", "系统提示词强调要围绕群友的话题回复"))
        if db.get("low_relevance", 0) > 20:
            quality_issues.append(("大量回复与消息无关", "群友说了A，糖糖回B", "检查上下文注入是否正确聚焦到当前说话人"))
        if db.get("follow_up_rate", 100) < 30:
            quality_issues.append(("回复后冷场率高", "糖糖回了但没人想接话", "回复要留钩子：提问、抛观点、邀请回应"))
        if db.get("memory_ref_rate", 0) < 5:
            quality_issues.append(("很少引用记忆", "记住了但不说出来，等于没记", "提示词强调在对话中自然提及记得的事"))
        if db.get("duplicate_replies", 0) > 3:
            quality_issues.append(("模板化回复", "同一句话对不同人说了多次", "系统提示词强调个性化回复"))
        if db["question_rate"] < 15:
            quality_issues.append(("几乎不提问", "不会反问=不会聊天", "提示词加：每次回复结束时尽量带一个问题"))

        if quality_issues:
            for label, problem, fix in quality_issues:
                print(f"     ⚠️ {label}")
                print(f"       问题：{problem}")
                print(f"       改进：{fix}")
                print()
        else:
            print(f"   ✅ 内容质量无明显问题")

        # 低质量样本
        if db.get("low_quality_samples"):
            print(f"   📝 低相关性回复示例（糖糖可能跑题了）：")
            for i, (human, bot) in enumerate(db["low_quality_samples"], 1):
                print(f"     {i}. 群友: {human}")
                print(f"        糖糖: {bot}")
                print()

        if db.get("reply_samples"):
            print(f"   📝 最近回复样本：")
            for i, sample in enumerate(db["reply_samples"][:3], 1):
                print(f"     {i}. {sample}...")

    # ── 核心诊断：消息 → 回复全链路追踪 ──
    print(f"\n📈 核心诊断：消息回复链路")
    print(f"   收到群消息: {s['total_group_msg']} 条")
    print(f"   群消息发送: {s['reply_sent']} 次")
    print(f"   LLM 调用: {s['llm_calls']} 次, 失败: {s['llm_failures']} 次")
    print(f"   插话状态: {'🟢 ON' if s['interjection_on'] else '🔴 OFF'}")

    # 主矛盾：被叫到也不回
    called = s["at_bot"] + s["name_mention"]
    llm_replied = s["llm_calls"]
    if called > 0:
        actual_rate = round(llm_replied / called * 100) if called else 0
        print(f"   被呼叫(@+提名字): {called} 次, LLM生成回复: {llm_replied} 次, 呼叫响应率: {actual_rate}%")

    # 主要矛盾检测
    if called >= 3 and llm_replied < called * 0.5:
        missed = called - llm_replied
        print(f"\n   🔴 主要矛盾：有人叫了她 {called} 次，她只回了 {llm_replied} 次，{missed} 次没回应！")
        print(f"   ═══ 逐层排查 ═══")

        # 第一层：消息有没有进到处理流程？
        if s["whitelist_blocked"] > 0:
            print(f"   ❌ 白名单拦截 {s['whitelist_blocked']} 条 → 群不在 config.yaml 的 groups 里")
            suggestions.append("→ 把群号加到 config.yaml → groups")
        if s["testing_blocked"] > 0:
            print(f"   ❌ 测试模式拦截 {s['testing_blocked']} 条 → testing_mode 开着")
            suggestions.append("→ config.yaml → testing_mode: false")

        # 第二层：进来了但被忙线锁拦了？
        if s["busy_skipped"] > 3:
            print(f"   ❌ 忙线跳过 {s['busy_skipped']} 次 → LLM 调用期间新消息被丢弃")
            suggestions.append("→ 群太活跃，增加冷却 /冷却 20")

        # 第三层：LLM 调用失败？
        if s["llm_calls"] > 0 and s["llm_failures"] > 0:
            fail_rate = round(s["llm_failures"] / max(1, s["llm_calls"]) * 100)
            if fail_rate > 5:
                print(f"   ❌ LLM 失败率 {fail_rate}% → API 有问题")
                suggestions.append("→ 检查 API Key 和余额")

        # 第四层：LLM 调了但回复没发出去？
        if s["llm_calls"] > 0 and s["group_send_fail"] > 0:
            print(f"   ❌ 生成回复但发送失败 {s['group_send_fail']} 次")
            suggestions.append("→ 可能被QQ风控，降低发送频率")

        # 第五层：呼叫根本就没被识别？
        if called > 0 and s["llm_calls"] < called * 0.3:
            if not s["interjection_on"] and s["name_mention"] > 0:
                print(f"   ❌ 名字触发跟插话开关绑定了！（已修复的bug可能还在）")
                print(f"      提名字{ s['name_mention'] }次但插话关了就无法触发")
                suggestions.append("→ 重启糖糖加载最新代码（_has_bot_nickname 已解绑）")

        # 第六层：回复率正常偏低但没有阻塞
        if s["interjection_on"] and s["interjection_triggers"] == 0:
            print(f"   ℹ️ 插话开着但从未触发 → 饥渴度可能太低或消息类型不合适")

        if s["conversation_enders"] > 5:
            print(f"   ℹ️ {s['conversation_enders']} 条终结性消息（嗯哦好）→ 不插话是正常的")

        print(f"   ════════════")

    # 摘要
    if not s["interjection_on"]:
        print(f"\n💡 根因判断：主动插话关闭，加上之前名字触发绑在插话开关上（已修复），导致 @和提名字都无法触发回复。")
        print(f"   解决方案：/插话 on + 重启糖糖")

    if db.get("unnamed", 0) > 5:
        issues.append(f"⚠️ {db['unnamed']} 个群友没有昵称")

    if db["memory_count"] < 10 and db["chat_count"] > 100:
        issues.append("⚠️ 聊天多但记忆少，auto_learn 可能不够活跃")
        suggestions.append("→ 没关系，LLM 每15条消息会提取一次")

    # 群活跃排行
    if db.get("group_activity"):
        print(f"\n📊 群活跃排行：")
        for gid, cnt, last in db["group_activity"][:5]:
            print(f"   群{gid}: {cnt} 条消息, 最后 {last}")

    # ── 输出问题 ──
    if issues:
        print(f"\n⚠️ 发现 {len(issues)} 个问题：")
        for i in issues:
            print(f"   {i}")
        if suggestions:
            print(f"\n💡 建议：")
            for sug in suggestions:
                print(f"   {sug}")
    else:
        print(f"\n✅ 未发现明显问题，糖糖运行正常！")

    # ── 最近错误 ──
    if s["errors"]:
        print(f"\n📛 最近错误（{len(s['errors'])} 条，显示最后5条）：")
        for typ, ts, msg in s["errors"][-5:]:
            print(f"   [{ts}] {typ}: {msg[:100]}")

    print(f"\n{'=' * 50}")
    print("诊断完成。")


def _is_recent(line):
    """判断日志行是否在最近1小时内"""
    m = re.search(r'^\[(\d{2}):(\d{2}):(\d{2})\]', line)
    if not m:
        return False
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    now = datetime.now()
    log_time = now.replace(hour=h, minute=mi, second=s, microsecond=0)
    if log_time > now:
        log_time -= timedelta(days=1)
    return (now - log_time).total_seconds() < 3600


if __name__ == "__main__":
    diagnose()
