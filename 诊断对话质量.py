"""
糖糖对话质量自动诊断工具

每次运行扫描最近的聊天记录，自动检测：
- 瞎编/假设（"明天上课""你在写代码"等糖糖不知道的事）
- 模板重复（同一开头用太多次）
- 括号动作泄露（"（笑）（眨眼）"等违反铁律）
- 知识/记忆污染（无关知识注入对话）
- 回复质量（太短/太长/纯表情包）

用法：python 诊断对话质量.py [--limit 200] [--fix]
"""

import sqlite3
import re
import sys
from collections import Counter
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path("memory.db")

# ═══════════════════════════════════════════
# 检测规则
# ═══════════════════════════════════════════

# 糖糖不该说的话——它不知道对方的具体日程/状态
FABRICATION_PATTERNS = [
    (r'明天(?:还要|要)?上课', '假设对方是学生'),
    (r'明天(?:还要|要)?上班', '假设对方要上班'),
    (r'明天(?:还要|要)?考试', '假设对方要考试'),
    (r'又[在搞]?写代码', '假设对方在写代码'),
    (r'还在[打搞]游戏', '假设对方在打游戏'),
    (r'(?:刚|在|又).{0,5}(?:加班|打工|搬砖)', '假设对方在加班/打工'),
    (r'(?:作业|论文).{0,5}(?:写|做|赶).{0,3}[完了没]', '假设对方有作业'),
    (r'你一[天个].{0,5}(?:忙|累)', '假设对方一天的状态'),
]

# 括号/符号动作——铁律禁止（含所有变体：（）【】[] **）
PAREN_ACTION = re.compile(r'[（(【\[][^)）】\]\n]{2,30}[)）】\]]|\*[^*\n]{2,20}\*')

# 开头词重复阈值
STARTS_REPEAT_THRESHOLD = 3  # 同一开头出现超过3次→警告

# 回复过短/过长
TOO_SHORT = 3   # 少于3字（扣除CQ码后）
TOO_LONG = 400  # 超过400字

# 疑似套路模板
TEMPLATE_PATTERNS = [
    (r'这么晚还(?:不睡|没睡)', '深夜模板：这么晚还不睡'),
    (r'(?:想我|睡不着|有心事)', '深夜模板：想我/睡不着'),
    (r'大半夜的', '套路用词：大半夜的'),
    (r'嘿嘿.*[~～]', '套路开头：嘿嘿'),
    (r'^(?:在呢|来了|嗯[呢呐])', '短应答开头'),
]

# ═══════════════════════════════════════════
# 诊断逻辑
# ═══════════════════════════════════════════


def load_bot_replies(conn, limit=200):
    """加载最近的糖糖回复"""
    rows = conn.execute('''
        SELECT id, message, qq_id, group_id, timestamp
        FROM chat_log
        WHERE is_bot_reply=1 AND message IS NOT NULL
        ORDER BY id DESC LIMIT ?
    ''', (limit,)).fetchall()
    return rows


def strip_cq(text):
    """移除CQ码"""
    return re.sub(r'\[CQ:[^\]]+\]', '', text).strip()


def check_fabrication(reply_text):
    """检测瞎编/假设"""
    hits = []
    for pat, desc in FABRICATION_PATTERNS:
        if re.search(pat, reply_text):
            hits.append((desc, re.search(pat, reply_text).group()))
    return hits


def check_paren_action(reply_text):
    """检测括号动作"""
    return PAREN_ACTION.findall(reply_text)


def check_templates(reply_text):
    """检测套路模板"""
    hits = []
    for pat, desc in TEMPLATE_PATTERNS:
        if re.search(pat, reply_text):
            hits.append(desc)
    return hits


def check_length(reply_text):
    """检测回复长度"""
    clean = strip_cq(reply_text)
    issues = []
    if len(clean) < TOO_SHORT:
        issues.append(f'太短({len(clean)}字)')
    if len(clean) > TOO_LONG:
        issues.append(f'太长({len(clean)}字)')
    return issues


def diagnose(limit=200):
    """主诊断函数"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    replies = load_bot_replies(conn, limit)
    if not replies:
        print("没有找到糖糖的回复记录。")
        return

    print(f"📊 扫描最近 {len(replies)} 条糖糖回复...\n")

    issues_found = []
    starts_counter = Counter()
    total_fabrications = 0
    total_paren = 0
    total_templates = 0
    replies_with_issues = 0

    for r in replies:
        clean = strip_cq(r['message'])
        if not clean:
            continue

        reply_issues = []

        # 1. 瞎编检测
        fabs = check_fabrication(clean)
        if fabs:
            total_fabrications += len(fabs)
            for desc, match in fabs:
                reply_issues.append(f'  🔴 瞎编({desc}): "{match}"')

        # 2. 括号动作
        parens = check_paren_action(clean)
        if parens:
            total_paren += len(parens)
            for p in parens[:2]:
                reply_issues.append(f'  🟡 括号动作: "{p}"')

        # 3. 模板套路
        temps = check_templates(clean)
        if temps:
            total_templates += len(temps)
            for t in temps:
                reply_issues.append(f'  🟠 模板: {t}')

        # 4. 长度
        lens = check_length(clean)
        for l in lens:
            reply_issues.append(f'  ⚪ {l}')

        if reply_issues:
            replies_with_issues += 1
            ts = r['timestamp'][:19] if r['timestamp'] else '?'
            target = f"群{r['group_id']}" if r['group_id'] else f"私聊{r['qq_id']}"
            preview = clean[:80].replace('\n', ' ')
            print(f'[{ts}] {target}: {preview}...')
            for issue in reply_issues:
                print(issue)
            print()

        # 收集开头词
        first_words = clean[:4]
        if first_words:
            starts_counter[first_words] += 1

    # ═══ 汇总 ═══
    print('=' * 60)
    print('📋 诊断汇总')
    print('=' * 60)

    # 瞎编统计
    if total_fabrications:
        print(f'\n🔴 瞎编/假设: {total_fabrications} 次 (在 {replies_with_issues} 条回复中)')
    else:
        print(f'\n✅ 未检测到瞎编/假设')

    # 括号动作统计
    if total_paren:
        print(f'\n🟡 括号动作: {total_paren} 次')
    else:
        print(f'\n✅ 未检测到括号动作')

    # 模板统计
    if total_templates:
        print(f'\n🟠 模板套路: {total_templates} 次')

    # 开头词重复
    print(f'\n📝 最常见开头词 (≥{STARTS_REPEAT_THRESHOLD}次=警告):')
    has_repeats = False
    for phrase, n in starts_counter.most_common(15):
        flag = ' ⚠️ 重复过多' if n >= STARTS_REPEAT_THRESHOLD else ''
        if flag:
            has_repeats = True
        print(f'  {n:4d}x  {phrase!r}{flag}')
    if not has_repeats:
        print(f'  (无重复开头)')

    # 🆕 记忆质量快检
    print(f'\n🧠 记忆质量快检:')
    mem_count = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
    spam_count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE value LIKE '%今日老婆%' OR value LIKE '%确定老婆%' OR value LIKE '%我是Q群管家%'"
    ).fetchone()[0]
    short_count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE length(value) < 10"
    ).fetchone()[0]
    print(f'  总记忆: {mem_count}')
    print(f'  疑似垃圾: {spam_count} (今日老婆/群管家等)')
    print(f'  过短(<10字): {short_count}')

    # 质量评分
    score = 100
    score -= min(total_fabrications * 10, 40)   # 每条瞎编 -10，最多 -40
    score -= min(total_paren * 3, 15)            # 每个括号 -3，最多 -15
    score -= min(total_templates * 5, 20)        # 每个模板 -5，最多 -20
    score -= min(spam_count, 25)                 # 每条垃圾 -1，最多 -25

    print(f'\n🏆 综合质量评分: {score}/100')
    if score >= 90:
        print('   优秀 — 只有小问题')
    elif score >= 70:
        print('   良好 — 有改进空间')
    elif score >= 50:
        print('   一般 — 建议针对性修复')
    else:
        print('   需要关注 — 多个严重问题')

    conn.close()
    return score


def fix_memory_spam():
    """清理已知的垃圾记忆"""
    conn = sqlite3.connect(str(DB_PATH))
    spam_conditions = [
        "value LIKE '%今日老婆%'",
        "value LIKE '%确定老婆%'",
        "value LIKE '%换老婆%'",
        "value LIKE '%老婆背包%'",
        "value LIKE '%群友老婆%'",
        "value LIKE '%抽取的老婆%'",
        "value LIKE '%我是Q群管家%'",
        "value LIKE '%群管家%'",
        "value LIKE '%/今日%'",
        "value LIKE '%老婆id%'",
    ]
    where = ' OR '.join(spam_conditions)
    before = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
    spam = conn.execute(f'SELECT COUNT(*) FROM memories WHERE {where}').fetchone()[0]
    if spam:
        conn.execute(f'DELETE FROM memories WHERE {where}')
        conn.commit()
    after = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
    print(f'记忆清理: {before} → {after} (删除 {spam} 条垃圾)')
    conn.close()


if __name__ == '__main__':
    limit = 200
    do_fix = False

    for arg in sys.argv[1:]:
        if arg == '--fix':
            do_fix = True
        elif arg.startswith('--limit='):
            limit = int(arg.split('=')[1])

    if do_fix:
        fix_memory_spam()
        print()

    try:
        diagnose(limit)
    finally:
        input("\n按 Enter 键退出...")
