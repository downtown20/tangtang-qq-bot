"""
🍬 糖糖全面诊断脚本
检查所有子系统的健康状态——内存、数据库、配置一致性。
用法：python 全面诊断.py
"""

import sys, os, json, sqlite3, re, asyncio
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

BASE = Path(__file__).parent
DB = BASE / "memory.db"
CFG = BASE / "config.yaml"
LOG = BASE / "tangtang.log"
STATE = BASE / ".catch_up_state.json"
SELF = BASE / ".tangtang_self.json"

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"
INFO = "ℹ️"

def header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def check(name: str, condition: bool, detail: str = "") -> bool:
    mark = PASS if condition else FAIL
    msg = f"  {mark} {name}"
    if detail and not condition:
        msg += f" — {detail}"
    print(msg)
    return condition

# ═══════════════════════════════════════
# 1. 文件完整性
# ═══════════════════════════════════════
header("1. 文件完整性")

check("config.yaml 存在", CFG.exists())
check("memory.db 存在", DB.exists())
check("tangtang.log 存在", LOG.exists())
check("role_card.md 存在", (BASE / "role_card.md").exists())

# 检查新模块
for mod in ["self_state", "context_builder", "reranker", "reflection"]:
    check(f"agent/{mod}.py 存在", (BASE / "agent" / f"{mod}.py").exists())

# 检查模型文件
bge_model = False
for search_dir in [BASE / "models", Path.home() / "models"]:
    for d in search_dir.glob("BAAI/bge-small-zh*"):
        if (d / "config.json").exists():
            bge_model = True
            break
    if bge_model:
        break
check("BGE embedding 模型已下载", bge_model, "运行 modelscope 下载 bge-small-zh-v1.5")

reranker_model = (BASE / "models" / "BAAI" / "bge-reranker-v2-m3" / "config.json").exists()
check("Reranker 模型已下载", reranker_model, "运行 modelscope 下载 bge-reranker-v2-m3")

# ═══════════════════════════════════════
# 2. 配置文件一致性
# ═══════════════════════════════════════
header("2. 配置文件一致性")

try:
    import yaml
    cfg = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    check("config.yaml 格式正确", isinstance(cfg, dict))
except Exception as e:
    print(f"  {FAIL} config.yaml 解析失败: {e}")
    cfg = {}

# testing_mode
testing = cfg.get("behavior", {}).get("testing_mode", False)
check("testing_mode 已关闭", not testing, "测试模式开着——非主人的消息全被静默丢弃！")

# 黑名单一致性
bl_groups = set(str(g) for g in cfg.get("blacklist", {}).get("groups", []))
groups_config = set(str(g) for g in (cfg.get("groups") or {}).keys())
overlap = bl_groups & groups_config
check("黑名单和 groups 配置无冲突", not overlap,
      f"以下群同时在白名单和黑名单中: {overlap}")

# ═══════════════════════════════════════
# 3. 数据库健康
# ═══════════════════════════════════════
header("3. 数据库健康")

try:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
except Exception as e:
    print(f"  {FAIL} 无法打开数据库: {e}")
    conn = None

if conn:
    # 表存在性
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for t in ["people", "memories", "chat_log", "fact_clusters", "group_info",
              "memory_embeddings", "daily_digests", "tangtang_journal", "feedback"]:
        check(f"表 {t} 存在", t in tables)

    # 数据量
    people_n = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    mem_n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    chat_n = conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]
    print(f"  {INFO} people: {people_n}人, memories: {mem_n}条, chat_log: {chat_n}条")

    check("有群友数据", people_n > 0, "people 表为空——糖糖还没和任何人互动过")

    # memory_embeddings 覆盖率
    if mem_n > 0:
        emb_n = conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings"
        ).fetchone()[0]
        emb_pct = round(emb_n / mem_n * 100) if mem_n else 0
        check(f"memory_embeddings 覆盖率", emb_pct >= 80,
              f"{emb_n}/{mem_n} ({emb_pct}%)——BGE 语义搜索对 {mem_n - emb_n} 条记忆无效")
        if emb_pct < 100 and emb_pct > 0:
            print(f"    → 糖糖启动后会自动填充缺失的 embedding")

    # daily_digests
    digest_n = conn.execute("SELECT COUNT(*) FROM daily_digests").fetchone()[0]
    journal_n = conn.execute("SELECT COUNT(*) FROM tangtang_journal").fetchone()[0]
    print(f"  {INFO} daily_digests: {digest_n}条, tangtang_journal: {journal_n}条")
    if digest_n == 0 and chat_n > 100:
        print(f"    → 情节记忆为空——反思整合会在糖糖启动后自动运行")

    # group_info vs 实际活跃群
    db_groups = set(str(r[0]) for r in conn.execute("SELECT group_id FROM group_info").fetchall())
    active_groups = set()
    for r in conn.execute(
        "SELECT DISTINCT group_id FROM chat_log WHERE group_id != '' AND is_bot_reply = 0"
    ).fetchall():
        active_groups.add(r[0])
    stale = db_groups - active_groups
    config_groups = set(str(g) for g in (cfg.get("groups") or {}).keys())
    stale_config = config_groups - active_groups
    check("group_info 表无过期群", len(stale) == 0,
          f"以下群在 group_info 里但 chat_log 里没有消息（可能已退出）: {stale}")
    check("config.yaml 无过期群配置", len(stale_config) == 0,
          f"以下群在 config.yaml 但 chat_log 无消息: {stale_config}")

    # 最近聊天活跃度
    recent = conn.execute(
        "SELECT group_id, COUNT(*) as cnt, MAX(timestamp) as last "
        "FROM chat_log WHERE group_id != '' AND is_bot_reply = 0 "
        "GROUP BY group_id ORDER BY last DESC"
    ).fetchall()
    if recent:
        print(f"  {INFO} 最近活跃群:")
        for r in recent[:6]:
            print(f"      群{r[0]}: {r[1]}条消息, 最后 {r[2]}")

    conn.close()

# ═══════════════════════════════════════
# 4. 状态文件
# ═══════════════════════════════════════
header("4. 状态文件")

if STATE.exists():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        last_offline = state.get("last_offline", "")
        last_online = state.get("last_online", "")
        print(f"  {INFO} last_offline: {last_offline}")
        print(f"  {INFO} last_online: {last_online}")
        if last_offline:
            try:
                offline_t = datetime.strptime(last_offline, "%Y-%m-%d %H:%M:%S")
                gap_h = (datetime.now() - offline_t).total_seconds() / 3600
                check("last_offline 在 6 小时内（未被手动杀进程导致过期）",
                      gap_h < 6,
                      f"已过期 {gap_h:.0f} 小时——重启时 catch_up 会用兜底窗口")
            except ValueError:
                check("last_offline 格式正确", False, f"无法解析: {last_offline}")
    except Exception:
        check("catch_up_state 可解析", False)
else:
    check("catch_up_state 不存在（首次运行正常）", True)

if SELF.exists():
    try:
        self_data = json.loads(SELF.read_text(encoding="utf-8"))
        sn = self_data.get("self_narrative", {}).get("summary", "")
        print(f"  {INFO} 自我叙事: {len(sn)}字")
        vals = self_data.get("values", {})
        if vals:
            print(f"  {INFO} 价值倾向: " + ", ".join(f"{k}={v:.2f}" for k, v in vals.items()))
    except Exception:
        check("tangtang_self 可解析", False)
else:
    print(f"  {INFO} tangtang_self.json 不存在（首次运行正常）")

# ═══════════════════════════════════════
# 5. 日志诊断
# ═══════════════════════════════════════
header("5. 最近日志")

if LOG.exists():
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    recent = [l for l in lines[-500:] if l.strip()]

    # 错误计数
    errors = [l for l in recent if "ERROR" in l or "Traceback" in l]
    warnings = [l for l in recent if "WARNING" in l]
    check("最近 500 行无 ERROR", len(errors) == 0,
          f"发现 {len(errors)} 条错误")
    if errors:
        print(f"    最近 3 条错误:")
        for e in errors[-3:]:
            print(f"      {e[:150]}")

    # 关键模块日志
    for tag, label in [
        ("🍬 糖糖的持久自我状态", "TangTangSelf 初始化"),
        ("✅ BGE 语义向量就绪", "BGE Embedding 加载"),
        ("✅ Reranker 就绪", "Reranker 加载"),
        ("🧠 批量填充", "Memory Embedding 填充"),
        ("📋 群信息已刷新", "群列表刷新"),
        ("📬 离线消息补读完成", "离线补读"),
        ("📬 补回复 →", "补回复发送"),
        ("🧘 反思整合完成", "反思整合"),
    ]:
        found = any(tag in l for l in recent)
        check(label, found, f"日志中未找到 '{tag}'——该功能可能未运行")

    # 关键错误模式
    for pattern, desc in [
        ("name '.*' is not defined", "存在未定义变量 (NameError)"),
        ("has no attribute", "存在属性缺失 (AttributeError)"),
        ("导入失败|import.*failed|ModuleNotFound", "模块导入失败"),
        ("LLM调用失败|LLM回复失败", "LLM 调用失败"),
    ]:
        matches = [l for l in recent if re.search(pattern, l)]
        check(f"无 '{desc}' 类错误", len(matches) == 0,
              f"发现 {len(matches)} 条")

    # 群列表刷新结果
    refresh_lines = [l for l in recent if "群信息已刷新" in l]
    if refresh_lines:
        for rl in refresh_lines[-1:]:
            print(f"  {INFO} {rl.strip()[-200:]}")

else:
    print(f"  {WARN} tangtang.log 不存在——糖糖可能尚未启动")

# ═══════════════════════════════════════
# 6. catch_up 补回复逻辑验证
# ═══════════════════════════════════════
header("6. catch_up 补回复验证")

if LOG.exists() and DB.exists():
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    recent = [l for l in lines[-500:] if l.strip()]

    # 检查 catch_up 是否在群列表就绪后运行
    refresh_done = any("群信息已刷新" in l for l in recent)
    catchup_done = any("离线消息补读完成" in l for l in recent)
    catchup_reply = any("补回复 →" in l for l in recent)

    if refresh_done and catchup_done:
        check("catch_up 在群列表刷新后运行", True)
    elif catchup_done:
        check("catch_up 在群列表刷新后运行", False,
              "catch_up 跑了但群列表可能还没就绪——检查日志顺序")

    if catchup_reply:
        print(f"  {INFO} 补回复已发送——离线期间的 @ 点名被正确回应")
    else:
        print(f"  {INFO} 无补回复发送——可能离线期间没人 @ 糖糖，或窗口内无匹配")

    # 检查 last_offline 是否过期
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
            last_off = state.get("last_offline", "")
            if last_off:
                off_t = datetime.strptime(last_off, "%Y-%m-%d %H:%M:%S")
                gap = (datetime.now() - off_t).total_seconds() / 60
                if gap > 60:
                    check("离线窗口合理（不是几个月前的旧值）", False,
                          f"last_offline 是 {gap:.0f} 分钟前——如果超过预期，说明 record_offline() 没被调用")
        except Exception:
            pass

# ═══════════════════════════════════════
# 7. handler.py 结构完整性
# ═══════════════════════════════════════
header("7. handler.py 结构完整性")

handler_path = BASE / "agent" / "handler.py"
if handler_path.exists():
    content = handler_path.read_text(encoding="utf-8", errors="replace")

    # 检查关键方法/属性定义
    for name, desc in [
        ("self_state = TangTangSelf", "TangTangSelf 初始化"),
        ("context_builder = ContextBuilder", "ContextBuilder 初始化"),
        ("reranker = RerankerEngine", "Reranker 初始化"),
        ("reflection = ReflectionEngine", "ReflectionEngine 初始化"),
        ("_record_feedback", "反馈追踪方法"),
        ("_extract_self_memories_llm", "LLM 自我记忆提取"),
        ("_bge_pick_memories", "BGE 记忆筛选"),
        ("_reflection_loop", "反思循环"),
        ("search_episodes", "情节记忆搜索工具"),
    ]:
        check(f"包含 {desc}", name in content, "代码可能未保存或结构损坏")

    # 检查 __init__ 是否被意外截断
    # 真正的问题：在 __init__ 的 8 空格缩进块中，出现了一个 4 空格缩进的 def
    # 正常情况：__init__ 结束 → 下一个 class-level def 也是 4 空格（与 __init__ 同级）
    lines = content.splitlines()
    in_init = False
    init_def_indent = 0  # __init__ 自身 def 的缩进（4空格 = class级）
    init_body_indent = 0  # __init__ 内部代码的缩进（8空格）
    found_issue = False
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("def __init__"):
            in_init = True
            init_def_indent = len(line) - len(stripped)
        elif in_init and not stripped.startswith("#") and not stripped.startswith('"""'):
            current_indent = len(line) - len(stripped)
            if init_body_indent == 0 and current_indent > init_def_indent and stripped:
                init_body_indent = current_indent  # 第一条非空非注释行 = body缩进
            # 检测：一个 def 出现在 body 缩进位置（嵌在 __init__ 里）
            if stripped.startswith("def ") and current_indent <= init_def_indent:
                # 同级的 def → __init__ 正常结束
                in_init = False
            elif stripped.startswith("def ") and current_indent < init_body_indent and current_indent > init_def_indent:
                check(f"__init__ 结构完整（行{i}未截断）", False,
                      f"第{i}行 '{stripped[:40]}' 缩进 {current_indent} 空格，"
                      f"介于 class 级({init_def_indent})和 body 级({init_body_indent})之间——"
                      f"__init__ 可能被意外截断！")
                found_issue = True
                break
    if not found_issue:
        check("__init__ 结构完整（未被意外截断）", True)

# ═══════════════════════════════════════
# 8. 模块导入测试
# ═══════════════════════════════════════
header("8. 模块导入测试")

for mod_path, desc in [
    ("agent.self_state", "TangTangSelf"),
    ("agent.context_builder", "ContextBuilder"),
    ("agent.reranker", "RerankerEngine"),
    ("agent.reflection", "ReflectionEngine"),
    ("agent.memory", "MemorySystem"),
    ("agent.personality", "PersonalityEngine"),
    ("agent.catch_up", "CatchUpManager"),
]:
    try:
        __import__(mod_path)
        check(f"导入 {desc}", True)
    except Exception as e:
        check(f"导入 {desc}", False, str(e)[:100])

# ═══════════════════════════════════════
header("诊断完成")
print("\n运行建议：")
print("  1. 所有 ❌ 项需要在重启糖糖前修复")
print("  2. ⚠️ 项建议修复但不阻塞运行")
print("  3. 启动糖糖后观察日志，确认无新的 ERROR/WARNING")
print("  4. 用小号在群里 @糖糖 做端到端测试")
print()
input("按回车键退出...")
