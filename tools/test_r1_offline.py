"""
R1 离线验证 —— 不依赖 QQ 连接，纯逻辑+数据库测试
覆盖: R1-1 认知类型体系, R1-2 工具方法类型, R1-3 Jinja2模板, R1-5 handler拆分
"""
import sys, os, sqlite3, tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from agent.store import Store
from agent.memory import MemorySystem, MemoryEntry
import agent.skills as skills_mod
from agent.handler_commands import CommandRouter

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  FAILED {detail}")

# ═══════════════════════════════════════
print("\n── R1-1 认知类型体系 ──")

# 临时数据库
db_fd, db_path = tempfile.mkstemp(suffix=".db")
os.close(db_fd)
store = Store(db_path)
mem = MemorySystem(store=store)

# 1a. 新字段插入
mid = mem.remember("100001", "fact", "在北大读计算机", importance=8,
                   cognitive="semantic", confidence=0.95, origin="extracted")
check("新字段插入有效ID", mid > 0)

mid2 = mem.remember("100001", "event", "上周去爬了香山", importance=5,
                    cognitive="episodic", confidence=1.0, origin="extracted")
check("episodic记忆插入", mid2 > 0)

# 1b. 查询透传
rows = store.query_memories("100001")
check("query返回结果", len(rows) >= 2)
sem = [r for r in rows if r.get("value") == "在北大读计算机"][0]
epi = [r for r in rows if r.get("value") == "上周去爬了香山"][0]
check("semantic cognitive", sem["cognitive"] == "semantic", str(sem.get("cognitive")))
check("episodic cognitive", epi["cognitive"] == "episodic", str(epi.get("cognitive")))
check("confidence透传", abs(epi["confidence"] - 1.0) < 0.01, str(epi.get("confidence")))
check("origin透传", sem["origin"] == "extracted", str(sem.get("origin")))

# 1c. MemoryEntry
entries = mem.recall("100001")
check("recall结果", len(entries) >= 2)
e_epi = [e for e in entries if e.cognitive == "episodic"][0]
check("MemoryEntry.cognitive", e_epi.cognitive == "episodic")
check("MemoryEntry.confidence", abs(e_epi.confidence - 1.0) < 0.01)
check("MemoryEntry.origin", e_epi.origin == "extracted")

# 1d. 旧格式默认值
mid3 = mem.remember("100002", "fact", "旧格式", importance=3)
rows3 = store.query_memories("100002")
check("默认cognitive", rows3[0].get("cognitive") == "semantic", str(rows3[0].get("cognitive")))
check("默认confidence", abs(float(rows3[0].get("confidence", 0.7)) - 0.7) < 0.01)
check("默认origin", rows3[0].get("origin") == "extracted", str(rows3[0].get("origin")))

# 1e. 各来源标记
mem.remember("100003", "profile_synthesis", "画像测试", importance=4, origin="summarized")
mem.remember("999999", "promise", "答应帮小明查资料", importance=8, origin="self")
mem.remember("100004", "said", "手动添加", importance=8, origin="manual")

for qid, expected in [("100003", "summarized"), ("999999", "self"), ("100004", "manual")]:
    r = store.query_memories(qid)
    check(f"{expected}来源", r[0].get("origin") == expected, str(r[0].get("origin")))

# 清理临时DB
store_path = db_path
try: os.unlink(store_path)
except: pass

# ═══════════════════════════════════════
print("\n── R1-1b 真实DB迁移 ──")
real_db = os.path.join(PROJECT, "memory.db")
if os.path.exists(real_db):
    # 实例化 Store 触发 ALTER TABLE 迁移
    real_store = Store(real_db)
    conn = sqlite3.connect(real_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
    check("cognitive列", "cognitive" in cols)
    check("confidence列", "confidence" in cols)
    check("origin列", "origin" in cols)
    try:
        row = conn.execute(
            "SELECT id, cognitive, confidence, origin FROM memories ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            print(f"  最新记忆 id={row[0]}: cognitive={row[1]}, confidence={row[2]:.2f}, origin={row[3]}")
    except sqlite3.OperationalError:
        print("  (记忆表为空，无数据可查)")
    conn.close()
else:
    print("  ⏭️ memory.db 不存在，跳过")

# ═══════════════════════════════════════
print("\n── R1-2 工具方法类型 ──")
get_method_type = skills_mod.get_method_type
list_skills_fn = skills_mod.list_skills
build_tools = skills_mod.build_tool_definitions

check("未注册默认agent", get_method_type("nonexistent") == "agent")

# 触发技能注册——各模块的 _register() 需要被调用
import agent.calculator; agent.calculator._register()
import agent.convert; agent.convert._register()
import agent.sensory; agent.sensory._register()
import agent.translate; agent.translate._register()
import agent.web_search; agent.web_search._register()
import agent.moments; agent.moments._register()
try:
    import agent.games; agent.games._register()
except Exception: pass
try:
    import agent.image_gen; agent.image_gen._register()
except Exception: pass
try:
    import agent.image_share; agent.image_share._register()
except Exception: pass

skills = {s.name: s for s in list_skills_fn()}
print(f"  已注册: {len(skills)} 个")

for name in ["calculate", "convert", "get_time", "get_weather", "web_search", "translate"]:
    if name in skills:
        check(f"{name}→agent", skills[name].method_type == "agent",
              skills[name].method_type)

tools = build_tools()
check("工具定义构建", len(tools) > 0)
print(f"  共 {len(tools)} 个工具可用")

# ═══════════════════════════════════════
print("\n── R1-3 Jinja2模板 ──")
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

tmpl_dir = Path(PROJECT) / "agent" / "prompts"
check("模板目录存在", tmpl_dir.is_dir())

if tmpl_dir.is_dir():
    env = Environment(loader=FileSystemLoader(str(tmpl_dir)), autoescape=False)
    check("system.j2存在", (tmpl_dir / "system.j2").is_file())

    tmpl = env.get_template("system.j2")
    empty = {k: "" for k in [
        "personality_base", "scenario_sensitivity", "scenario_tone",
        "time_context", "mood_context", "cot_text", "relationship_tier",
        "power_structure", "group_vibe", "relationship_context",
        "existence_context", "self_narrative", "drives_context",
        "memories", "topic_memories", "knowledge", "scene_instruction",
        "active_members", "preferences", "sticker_history",
        "song_list", "drift_context", "cast_context", "capability_note",
    ]}
    result = tmpl.render(**{**empty,
        "personality_base": "TEST_ROLE",
        "cot_text": "CoT测试",
        "relationship_tier": "关系：亲密",
        "group_vibe": "群风测试",
        "relationship_context": "关系感测试",
        "existence_context": "存在状态测试",
        "self_narrative": "自我叙事测试",
        "drives_context": "社交渴望：0.6",
        "capability_note": "能力说明",
    })
    check("personality", "TEST_ROLE" in result)
    check("CoT", "CoT测试" in result)
    check("关系", "亲密" in result)
    check("群风", "群风测试" in result)
    check("关系感", "关系感测试" in result)
    check("存在状态", "存在状态测试" in result)
    check("自我叙事", "自我叙事测试" in result)
    check("驱动力", "社交渴望" in result)
    check("能力", "能力说明" in result)
    print(f"  渲染: {len(result)} 字符")

    # 空值不渲染标题
    empty_min = {**empty, "personality_base": "MIN"}
    result_min = tmpl.render(**empty_min)
    for title in ["## 这个群的风格", "## 知识库", "### 活跃群友", "## 相关往事"]:
        check(f"无值时无'{title}'", title not in result_min)

# ═══════════════════════════════════════
print("\n── R1-5 handler拆分 ──")
check("CommandRouter导入", True)
check("handle方法", hasattr(CommandRouter, "handle"))
for method in ["_cmd_memory", "_cmd_status", "_cmd_help",
               "_cmd_personality", "_cmd_voice", "_cmd_intimacy"]:
    check(f"{method}存在", hasattr(CommandRouter, method))
check("DANGEROUS_CMDS", "/说" in getattr(CommandRouter, "_DANGEROUS_CMDS", set()))

# ═══════════════════════════════════════
print("\n── 需运行时验证 ──")
print("  R1-4 身份锚定: 糖糖说个人信息→不存成群友记忆→查memory.db")
print("  R1-6 @防抖: 2人同时@→只回1条→看控制台日志")
print("  R1-6 私聊合并: 连发3条→1次LLM→看控制台日志")
print("  R1-2 behavior: send_voice→日志'本轮全部为behavior工具'")
print("  R1-1 认知类型: 发消息→提取后查memory.db的cognitive字段")

# ═══════════════════════════════════════
print(f"\n{'='*50}")
print(f"结果: {passed} 通过, {failed} 失败")
if failed:
    print("❌ 有未通过项")
    sys.exit(1)
else:
    print("✅ 全部通过")
