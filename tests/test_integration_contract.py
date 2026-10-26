"""
接线审计测试（2026-08-15）——把 CLAUDE.md 教训从散文变成红灯。

背景：教训 #14「引擎在转，离合器没接上」三次复发（Reranker/驱动力/情绪标签），
散文清单拦不住。本测试机械断言三件事：

A. 零调用扫描：agent/ 模块的公共函数必须有生产调用方——
   调用（AST，别名感知）/ 属性访问 / 字符串引用 / @register_skill 注册都算接线。
   例外必须登记 ALLOWLIST 并写明理由（新增死代码 → 红灯）。
B. 管线契约：关键入口点必须在指定文件被调用（如驱动力投影进两条回复路径）。
C. 判定单一来源：handler.py 的插话判定只允许来自 interjection.evaluate()——
   2026-08-15 运行当天就抓出 80/65 两处 evaluate 外重判并已修复。

已知边界（文档化）：
- 纯互相调用的死模块不在此覆盖内（模块入口函数互相调用但整模块无人用）——
  需要端到端验证补位。
- 属性访问引用偏宽松（.close 会算 close() 的接线）——宁可漏报不可误报。
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
BASE = Path(__file__).parent.parent

# 生产调用方范围：agent/ onebot/ tools/ + 根目录 main.py 与控制台。
# tests/ 不算——测试调用不构成生产接线（test-only 函数 = 生产死代码）。
PROD_FILES = sorted(
    [BASE / "main.py", BASE / "糖糖控制台_qt.py"]
    + [p for d in ("agent", "onebot", "tools") for p in (BASE / d).rglob("*.py")]
)


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# key 统一为相对路径（正斜杠），如 "agent/handler.py"
_parsed = {}
for _p in PROD_FILES:
    _t = _parse(_p)
    if _t is not None:
        _parsed[str(_p.relative_to(BASE)).replace("\\", "/")] = _t


def _global_aliases(sources: dict | None = None) -> dict[str, str]:
    """全局别名表：from x import f as g / 模块级 target = func / self.x = func → 规范名。

    跨文件生效——如 self._dispatch = dispatch，别处 self._dispatch(...) 据此解析。
    （AST 静态扫描的第一课：别名绑定会藏调用点。）

    2026-10-13 P3 修复（扫描器读侧不对称的根因）：原实现 ast.walk **整个文件**，
    于是**函数内部的局部赋值**也被当成全局别名——实测 218 条 Name 目标里有 **210 条**
    来自函数体内（`clean = fname`、`energy = energy_val`、`text = transcript` …）。
    _canon 于是把真函数名改写掉（`clean`→`fname`），而 _references() 存键用规范名、
    查找却用裸名 → 真在被调用的 ReplyPipeline.clean / MoodEngine.energy 被判成零调用。
    现在 Name 目标**只认模块级**；Attribute 目标（self.x = y）保持全树——那才是本表
    存在的理由，且实测 320 条都是正当用法。

    `sources` 可注入（{文件名: ast.Module}）以便测试直接验证本函数，不必依赖全仓扫描。
    """
    m: dict[str, str] = {}
    for t in (sources or _parsed).values():
        # Name 目标只取模块级——函数内局部变量不是别名
        for node in t.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                for tg in node.targets:
                    if isinstance(tg, ast.Name):
                        m[tg.id] = node.value.id
        for node in ast.walk(t):
            if isinstance(node, ast.ImportFrom) and node.names:
                for a in node.names:
                    if a.asname:
                        m[a.asname] = a.name
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                for tg in node.targets:
                    if isinstance(tg, ast.Attribute):
                        m[tg.attr] = node.value.id
    return m


_ALIASES = _global_aliases()


def _canon(name: str) -> str:
    return _ALIASES.get(name, name)


def _record(d: dict[str, set[str]], raw: str, f: str) -> None:
    """同时按**原始名**与**规范名**登记一个引用。

    2026-10-13 P3：只按规范名登记，等于让「声明名能否查到」依赖别名表是干净的；
    而别名表**无法**区分「self.x = 函数」与「self.x = 数据字段」——实测
    `agent/mood.py:164  self._state.energy = energy_val` 就把公开名 energy 改写掉，
    `ReplyPipeline.clean` / `MoodEngine.energy` 因此被判成零调用（假红）。
    两个键都登记：声明名一定查得到，别名解析（pns → parse_natural_schedule）照旧。
    """
    d.setdefault(raw, set()).add(f)
    c = _canon(raw)
    if c != raw:
        d.setdefault(c, set()).add(f)


# 自引用防护名单（2026-10-14 台账 T2.2）：这些文件里的**字符串常量**不作为「引用证据」。
#
# 起因：`CLASS_METHOD_BASELINE` 是 72 个 ('文件', '方法名') 字符串对。若把 tests/ 纳入
# 扫描，这些字符串会进 `strs` 桶 —— 于是「基线条目本身」变成「该方法有字符串引用」的
# 证据，全部 72 条被判已接线，**基线永远无法腐坏告警**（教训 #30 变体：检查器被自己
# 的声明骗过）。当前 `_parsed` 不含 tests/，所以暂未暴露；这里防的是将来有人扩范围。
#
# 只挡**字符串**证据，不挡 AST 调用/属性证据——本文件若真调用了某方法，仍算数。
_SELF_REFERENCE_GUARD = {"tests/test_integration_contract.py"}


def _references(sources: dict | None = None) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """(调用, 属性访问, 字符串引用) 按原始名 + 规范名聚合 → 引用文件集合

    见 `_SELF_REFERENCE_GUARD`：闸门不得把自己的基线文本当成「已接线」的证据。
    """
    calls: dict[str, set[str]] = {}
    attrs: dict[str, set[str]] = {}
    strs: dict[str, set[str]] = {}
    for f, t in (sources or _parsed).items():
        for node in ast.walk(t):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    _record(calls, node.func.id, f)
                elif isinstance(node.func, ast.Attribute):
                    _record(calls, node.func.attr, f)
            elif isinstance(node, ast.Attribute):
                _record(attrs, node.attr, f)
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and f not in _SELF_REFERENCE_GUARD):
                _record(strs, node.value, f)
    return calls, attrs, strs


_CALLS, _ATTRS, _STRS = _references()


def _test_references() -> dict[str, set[str]]:
    """方法名 → 「在 tests/ 里被属性调用过」的测试文件集合（2026-10-23 T2.2）。

    **为什么需要它**（这是本轮踩出来的一个真错误，写在这里防复发）：
    零调用闸门的 `_parsed` **不包含 tests/**，所以它报的「零引用」只覆盖生产代码。
    而 `_pick_private_target` / `_settle_stale_seeks` 各有 4 处测试调用——
    按「闸门零引用」直接删掉，当场把 8 条测试打成 AttributeError。

    这正是教训 #36 的又一形态：**「工具报的 0 是关于工具的事实，不是关于代码的事实」**。
    闸门只看生产 ⇒ 它给的答案准确回答了「生产里有没有人调」，而不是
    「这个方法能不能删」——后者必须再问一句「测试里有人调吗」。

    所以本函数不是证据来源（不改变「是否零引用」的判定），而是**删除前的必查项**：
    基线条目里凡是「仅测试引用」的，都标出来，免得下一个人照着"零引用"去删。
    """
    out: dict[str, set[str]] = {}
    tests_dir = BASE / "tests"
    if not tests_dir.is_dir():
        return out
    for path in sorted(tests_dir.glob("*.py")):
        # 闸门自己的文件要排除——基线条目的字符串会把自己写成"有引用"（T2.2 自引用陷阱）
        if path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                out.setdefault(node.attr, set()).add(path.name)
    return out


_TEST_REFS = _test_references()


def _skill_registered() -> set[tuple[str, str]]:
    """(注册名, 文件)——@register_skill 装饰即接线（按名字分发，无静态调用点）"""
    reg: set[tuple[str, str]] = set()
    for f, t in _parsed.items():
        for node in ast.walk(t):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                        fn = dec.func
                        nm = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                        if nm == "register_skill":
                            reg.add((dec.args[0].value, f))
    return reg


_REG = _skill_registered()

# 例外登记——每个条目必须写明理由。只允许「标准工具函数保留待接线」类，
# 不允许「暂时没空接」。登记的函数被删除时 test_allowlist_entries_still_exist 会红灯。
ALLOWLIST: dict[tuple[str, str], str] = {
    ("agent/text_utils.py", "word_match"):
        "CLAUDE.md 七章规定的标准工具函数——新功能中文匹配接线时调用（word_match_any/word_match_score 已按反模式#6 归档）",
    ("agent/platform_receipts.py", "validate_platform_receipt"):
        "ADR-006 纯验证入口——平台回执适配器尚未接入生产路由，feature-off 期间保留契约和回归测试",
    # ── 2026-10-24 会客厅：HTTP 框架回调，不是「待接线」──
    # BaseHTTPRequestHandler 由 stdlib 的 handle_one_request() 按 HTTP 动词分发，
    # 静态分析永远看不到调用者。按「框架回调」登记，理由与上面两条（待接线）不同。
    ("agent/console_api.py", "do_GET"):
        "框架回调——stdlib BaseHTTPRequestHandler 按 HTTP 方法名分发，代码里不会有调用点",
    ("agent/console_api.py", "do_POST"):
        "框架回调——stdlib BaseHTTPRequestHandler 按 HTTP 方法名分发，代码里不会有调用点",
    ("agent/console_api.py", "do_DELETE"):
        "框架回调——stdlib BaseHTTPRequestHandler 按 HTTP 方法名分发，代码里不会有调用点",
    # ── 2026-10-26 图库打标：桌面端直调 + 测试钩子 ──
    # `agent/image_tagger.py` 的**生产调用方是 `tools/desktop_bridge.py`**，
    # 而零调用扫描只覆盖 agent/ 与入口，tools/ 不在它的图里 —— 所以这里登记，
    # 理由与「框架回调」同类：真的有调用者，只是扫描器看不见。
    ("agent/image_tagger.py", "load_config"):
        "桌面端 bridge 直调（tools/ 不在扫描范围）；每次读，改设置立刻生效",
    ("agent/image_tagger.py", "gallery_dir"):
        "桌面端 bridge 直调（tools/ 不在扫描范围）",
    ("agent/image_tagger.py", "metadata_path"):
        "桌面端 bridge 直调（tools/ 不在扫描范围）",
    ("agent/image_tagger.py", "scan"):
        "桌面端 bridge 直调：图库页「还剩多少张没打标」",
    ("agent/image_tagger.py", "status"):
        "桌面端 bridge 直调：配置 + 扫描 + 进度合一",
    ("agent/image_tagger.py", "run"):
        "桌面端 bridge 直调：图库页「打标」按钮",
    ("agent/image_tagger.py", "cancel"):
        "桌面端 bridge 直调：图库页「停止」",
    ("agent/image_tagger.py", "describe_one"):
        "桌面端 bridge 直调：设置页「试打一张」（不写 metadata）",
    ("agent/image_tagger.py", "reset_state_for_tests"):
        "测试钩子——把模块级进度/缓存清干净；生产代码不该调，静态扫描看不到测试调用",
    ("agent/console_api.py", "log_message"):
        "框架回调——BaseHTTPRequestHandler 内部调用的访问日志钩子（本处故意静音）",
}


# ── 类方法零调用基线（2026-10-13 P3，棘轮式冻结；主人拍板「非破坏性」）──
# 为什么需要它：零调用闸门（TestZeroCallerGate）只扫**模块级**函数，类方法完全不在
# 覆盖内——审计把这条称为「闸门有覆盖率上限」的元根因。扩到类方法后实测 agent/ 的
# 628 个公共方法里 **79 个零引用**（12.6%）。
#
# 为什么是「基线」而不是「直接接线或删除」：79 条里有真实依赖（如
# `Store.complete_send_outbox` 是出站回执终局入口，P2 可能要用），主人 2026-10-13
# 拍板先**非破坏性冻结**——不删、不接线，只让闸门拦住**新增**死方法。
#
# 棘轮语义（两条都是红灯，不是警告）：
#   ① 出现基线外的新零引用方法 → test_no_new_dead_class_methods 红；
#   ② 基线条目已失效（方法被删、或已被接线）→ test_baseline_entries_are_still_valid 红。
# 第 ② 条是防基线腐烂（教训 #35：文档不改就会静默失真）——接线了一条就删一条，
# 基线只许变小。
#
# 条目理由由取证脚本生成，分四类：观察器/健康查询（写好无消费者，正是 M5「观察器挂
# 计划任务 + 缺席告警」缺的那一半）、指标埋点、出站回执/终局入口（P2 依赖）、定时入口。
CLASS_METHOD_BASELINE: dict[tuple[str, str], str] = {
    ('agent/album_patrol.py', 'scan_and_like'): '仅测试引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/album_patrol.py', 'like_all_members'): '无任何引用',
    ('agent/context_builder.py', 'get_last_report'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/conversation_tracker.py', 'get_durable_health'): '仅测试引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/conversation_tracker.py', 'get_window_bonus'): '仅测试引用',
    ('agent/conversation_tracker.py', 'is_private_expired'): '仅测试引用',
    ('agent/daily_report.py', 'set_time'): '无任何引用；定时入口——CronScheduler/DailyReport 生产零使用（上一轮文档已取证）',
    ('agent/daily_report.py', 'get_schedule'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/drives.py', 'prediction_error'): '无任何引用',
    ('agent/drives.py', 'get_all_active'): '无任何引用',
    ('agent/group_style.py', 'get_group_name'): '无任何引用',
    ('agent/health_check.py', 'run_daily'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/health_check.py', 'self_state_context'): '仅测试引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/knowledge_index.py', 'snapshot'): '仅测试引用',
    ('agent/knowledge_index.py', 'get_document'): '仅测试引用',
    ('agent/knowledge_index.py', 'get_chunks'): '仅测试引用',
    ('agent/knowledge_index.py', 'load_semantic_segments'): '仅测试引用',
    ('agent/memory.py', 'mark_extracted'): '无任何引用',
    ('agent/memory.py', 'get_topic_context'): '无任何引用',
    ('agent/metrics.py', 'record_extract_success'): '无任何引用；指标埋点——记忆提取可观测性写好了但无上报点（写了没生效家族）',
    ('agent/metrics.py', 'record_extract_empty'): '无任何引用；指标埋点——记忆提取可观测性写好了但无上报点（写了没生效家族）',
    ('agent/metrics.py', 'record_extract_rejected'): '无任何引用；指标埋点——记忆提取可观测性写好了但无上报点（写了没生效家族）',
    ('agent/metrics.py', 'record_busy_skip'): '无任何引用；指标埋点——记忆提取可观测性写好了但无上报点（写了没生效家族）',
    ('agent/metrics.py', 'record_dedup_hit'): '无任何引用；指标埋点——记忆提取可观测性写好了但无上报点（写了没生效家族）',
    ('agent/metrics.py', 'get_latest'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/mood_tracker.py', 'on_alert'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/perception.py', 'get_weekly_report'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/platform_receipts.py', 'to_evidence'): '仅测试引用；出站回执/终局入口——主人 2026-10-13 拍板当「待接线」留着（P2 可能要用）',
    ('agent/reranker.py', 'last_load_error'): '仅测试引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/scenario.py', 'resolve_private_scenario'): '无任何引用',
    ('agent/scheduler.py', 'add_one_shot'): '仅测试引用；定时入口——CronScheduler/DailyReport 生产零使用（上一轮文档已取证）',
    ('agent/scheduler.py', 'add_weekly'): '仅测试引用；定时入口——CronScheduler/DailyReport 生产零使用（上一轮文档已取证）',
    ('agent/self_state.py', 'get_or_create_group_atmosphere'): '无任何引用',
    ('agent/self_state.py', 'update_group_atmosphere'): '无任何引用',
    # 2026-10-15：songs.py 的 search_by_emotion / find_section / get_section /
    # get_default_section 已随「歌词与分段唱歌」整体删除（见 agent/songs.py 模块说明）。
    # 棘轮自洽：生产侧一删，登记就必须跟着删——否则这里会因为「登记了不存在的方法」报红。
    # 同理本轮新删的 get_song_audio 取代了 get_section_audio，后者不再登记。
    # 2026-10-15：get_song / songs_with_status 已随生产侧一起归档（消费者是已删的
    # tools/rvc_sing.py 与控制台曲库设置页）。棘轮自洽生效——生产侧一删，本条就会因
    # 「登记了已不存在的方法」报红，逼着把这两条一起删掉，所以这里不留登记。
    ('agent/sticker.py', 'semantic_cache_stats'): '仅测试引用',
    ('agent/sticker.py', 'tag_sticker'): '无任何引用',
    ('agent/sticker.py', 'untag_sticker'): '无任何引用',
    ('agent/sticker.py', 'count_by_emotion'): '无任何引用',
    ('agent/sticker.py', 'list_by_emotion'): '无任何引用',
    ('agent/sticker.py', 'get_by_emotions'): '仅测试引用',
    ('agent/sticker.py', 'random_safe_sticker'): '仅测试引用',
    ('agent/store.py', 'find_all_people_sorted'): '无任何引用',
    ('agent/store.py', 'get_member_groups'): '无任何引用',
    ('agent/store.py', 'get_all_groups_info'): '无任何引用',
    ('agent/store.py', 'get_person_intimacy'): '仅测试引用',
    ('agent/store.py', 'complete_send_outbox'): '仅测试引用；出站回执/终局入口——主人 2026-10-13 拍板当「待接线」留着（P2 可能要用）',
    ('agent/store.py', 'mark_send_outbox_uncertain'): '仅测试引用；出站回执/终局入口——主人 2026-10-13 拍板当「待接线」留着（P2 可能要用）',
    ('agent/store.py', 'list_proactive_events'): '仅测试引用',
    ('agent/store.py', 'record_task_confirmation'): '仅测试引用',
    ('agent/store.py', 'get_task_ordinal_ledger'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/store.py', 'delete_memory_by_id'): '无任何引用',
    ('agent/store.py', 'replace_synthesis_memories'): '无任何引用',
    ('agent/store.py', 'expire_action_receipts'): '仅测试引用；出站回执/终局入口——主人 2026-10-13 拍板当「待接线」留着（P2 可能要用）',
    ('agent/store.py', 'quarantine_synthetic_chat_logs'): '仅测试引用',
    ('agent/store.py', 'unquarantine_chat_log'): '仅测试引用',
    ('agent/store.py', 'count_unprocessed_messages'): '仅测试引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/store.py', 'get_active_groups'): '无任何引用',
    ('agent/store.py', 'get_recently_active_chats'): '无任何引用',
    ('agent/store.py', 'search_similar_memories'): '无任何引用',
    ('agent/store.py', 'delete_embedding'): '无任何引用',
    ('agent/store.py', 'has_embeddings'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/store.py', 'count_episodes_for_user'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/store.py', 'get_max_chat_id'): '无任何引用',
    ('agent/store.py', 'get_mood_log'): '无任何引用；观察器/健康查询——查询写好了但无消费者（M5 待接定时观察任务 + 缺席告警）',
    ('agent/store.py', 'get_chat_max_ids_by_user'): '无任何引用',
    # ── 2026-10-24 未闭合事件（open_loops）L1：**分批落地，本批只有存储层** ──
    # 方案 docs/开发规划/未闭合事件_技术方案_20261024.md 把实施拆成 L1..L5，
    # L1 只建表+状态机，接线在 L2（提取识别）/L3（注入）/L4（收尾工具）/L5（过期清理）。
    # 这正是闸门要的「确认为待接线能力」——每接一条线就要删掉对应条目（棘轮）。
    #
    # ⚠ 2026-10-25 第 12 轮更正（连同教训 #42）：这里曾写着
    #   「✅ add_open_loop 已由 L2 接上（handler._persist_extracted_items）」
    # ——**那是假接线**。`_persist_extracted_items` 生产零调用（本文件
    # PRIVATE_METHOD_BASELINE 自己就把它登记为 T 类），生产提取早已改走
    # `Store.complete_extraction_job` → `_apply_extraction_item`。
    # 结果：open_loops 表 11145 个任务、**0 行**，而闸门还标着"已接"。
    # 现在真实接线在 `Store._apply_extraction_item` → `_record_open_loop_if_needed`
    # → `_upsert_open_loop_conn`（同一事务），由
    # `tests/test_open_loop_live_path.py` 端到端钉住（真跑 complete_extraction_job）。
    # 这条教训也说明了本闸门的边界：**它只查"符号有没有调用点"，不查
    # "那个调用点所在的方法可不可达"**——查后者要靠行为测试。
    #
    # ✅ 2026-10-25 第 13 轮：读侧三层全部接上，按棘轮删除三条登记：
    #   · `list_open_loops` → `handler._get_open_loops_context`（私聊每轮注入，limit=3）
    #   · `resolve_open_loop` → `_build_memory_tools` 的 resolve_open_loop 工具 +
    #     `_execute_tool` 分支（只给私聊，三条拒绝在 store 侧）
    #   · `expire_open_loops` → `handler_autonomy._autonomous_loop` 启动清理 +
    #     每日汇总两处调度（days_overdue=30, max_asked=2）
    # 行为由 `tests/test_open_loops_injection.py`（26 条）与端到端验证钉住。
    ('agent/ui_anim.py', 'transition_locked'): '无任何引用',
}


# ── 私有方法零调用基线（2026-10-23 T2.2，棘轮式冻结）──
#
# 为什么需要它（先说清为什么此前没有）：零调用闸门的类方法扫描一直带
# not name.startswith('_')，于是**私有方法全在覆盖外**——而 T2 归档的 7 条死代码
# **全是私有**。闸门没见过它们，等于这条防线只能管一半。
#
# T2.2 原登记的处置是「待拍板」：存量 15 条（9 条测试有调用、6 条真候选）。
# 2026-10-23 按项目宗旨自行决策扩了。理由：闸门的自引用缺陷（基线把自己当引用
# 证据）已在 T2.2 修好，扩覆盖不再有那个前提障碍；而「删掉的死代码闸门看不见」
# 是实打实的覆盖率缺口（教训 #37 那一类：验证尺子自己骗人）。
#
# 扩完实测 **14 条**（其中 4 条已由 T67 删除，基线现存 10 条）。⚠ **这里踩过一个真错误，写下来防复发**：
# 第一版按「闸门零引用」判成 14 条全是死代码，直接动手删了 handler_autonomy 的
# 两条——**当场把 8 条测试打成 AttributeError**。原因是本闸门的 _parsed
# **不包含 tests/**（见文件头注释：「tests/ 不算，test-only 函数 = 生产死代码」是
# 刻意的设计）。于是「闸门报零引用」只回答「生产里有没有人调」，**不回答**
# 「这个方法能不能删」。这正是教训 #36：**工具报的 0 是关于工具的事实，不是关于
# 代码的事实**。
#
# 所以基线分两类，处置完全不同：
#   D 类（真死）：生产零引用 **且** tests/ 零引用 ⇒ 可以直接删（4 条已由 T67 删除，基线里不再有）。
#   T 类（仅测试引用，10 条）：生产零引用但测试在调 ⇒ **删要连带改测试**，
#        属独立任务；其中 _record_task_confirmation_from_platform 根本不是死代码，
#        是 ADR-006 等接线的公开 API（与 T5 同族）。
# 判据由 test_dead_class_entries_have_no_test_refs 动态钉住——基线理由若腐烂即红。
PRIVATE_METHOD_BASELINE: dict[tuple[str, str], str] = {
    ('agent/handler.py', '_seductive_knowledge'): 'T 类（仅测试引用）——色色范本自 T2.14 起走 intimate_reference+pinned；但**测试仍在测它** ⇒ 删要连带改测试',
    ('agent/handler.py', '_extract_self_memories'): 'T 类（仅测试引用）——同步版被 _extract_self_memories_async 取代；测试仍调同步版',
    ('agent/handler.py', '_persist_extracted_items'): 'T 类（仅测试引用）——测试仍在调；生产零调用',
    ('agent/handler.py', '_analyze_and_index_document'): 'T 类（仅测试引用）——测试仍在调；生产零调用',
    ('agent/handler.py', '_get_profile_and_recent'): 'T 类（仅测试引用）——测试仍在调；生产零调用',
    ('agent/handler.py', '_search_people'): 'T 类（仅测试引用）——测试仍在调；与 LLM 工具 search_people 同名不同物（工具走 _execute_tool 分支）',
    ('agent/handler_autonomy.py', '_pick_private_target'): 'T 类（仅测试引用）——同步版被 _pick_private_target_async 取代；**测试 4 处调同步版**——本轮试删当场打红 4 条',
    ('agent/handler_autonomy.py', '_settle_stale_seeks'): 'T 类（仅测试引用）——同步版被 _settle_stale_seeks_async 取代；**测试 4 处调同步版**——本轮试删当场打红 4 条',
    ('agent/personalization.py', '_load_sent_today'): 'T 类（仅测试引用）——唯一生产调用点用 _load_sent_today_async；测试仍调同步版',
    ('agent/store.py', '_record_task_confirmation_from_platform'): 'T 类（仅测试引用）——**ADR-006 迟到确认 API**（有 _capability 能力令牌保护），**测试 11 处调用**；生产未接线（与 T5 同族）⇒ 这是等接线不是死代码',
}


def _agent_public_functions():
    for f in sorted(_parsed):
        if not f.startswith("agent/"):
            continue
        t = _parsed[f]
        for node in t.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                yield f, node.lineno, node.name


def _is_private_method_name(name: str) -> bool:
    """私有方法（_foo）但**排除 dunder**（__init__/__repr__ 等框架钩子）。

    2026-10-23 T2.2：dunder 由解释器/框架调用，静态引用集里天生看不到调用点，
    纳入闸门只会产生几十条无法消除的噪声（T2.2 记录实测 69 条）。
    """
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _class_public_methods(sources: dict | None = None, *, include_private: bool = False):
    """(文件, 行号, 类名, 方法名)——agent/ 类里的方法。

    2026-10-13 P3：零调用闸门此前只走 _agent_public_functions()（模块级），
    类方法完全不在覆盖内。这是审计说的「闸门有覆盖率上限」元根因。

    2026-10-23 T2.2（台账「私有方法扩覆盖待拍板」——按项目宗旨自行决策扩了）：
    include_private=True 时连私有方法一起扫（排除 dunder）。为什么必须扩：
    T2 归档的 7 条死代码**全是私有**，而当时的闸门从头到尾没见过它们——
    「删掉的死代码闸门看不见」等于这条防线只管一半。T2.2 的自引用缺陷
    （基线把自己当引用证据）当时已先修好，所以现在扩是安全的。
    """
    srcs = sources if sources is not None else _parsed
    for f in sorted(srcs):
        if not f.startswith("agent/"):
            continue
        for cls in ast.walk(srcs[f]):
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in cls.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    if include_private and _is_private_method_name(node.name):
                        yield f, node.lineno, cls.name, node.name
                    continue
                yield f, node.lineno, cls.name, node.name


def _zero_ref_class_methods(sources: dict | None = None, *,
                            include_private: bool = False
                            ) -> list[tuple[str, int, str, str]]:
    """真正零引用的类方法（默认只公共；include_private=True 时含私有，排除 dunder）。

    **不做**基线剔除——基线剔除是闸门（测试）的事，扫描器只报事实，
    这样「基线是否失效」才可被独立检验。技能注册/ALLOWLIST 视为已接线。

    2026-10-23 T2.2：私有方法与公共方法用**完全同一套证据模型**（属性 + 字符串，
    不吃裸名）。理由是同一批理由——私有方法也不可能靠裸标识符被调用到。
    """
    srcs = sources if sources is not None else _parsed
    # 2026-10-14（台账 T2.3）：**不吃裸名调用证据**。
    # 类方法必须经接收者到达——裸名 `snapshot()` 只可能指向模块级函数，
    # 不可能指向 `SomeClass.snapshot`。此前把裸名并进 calls 桶，导致
    # 任何文件里出现同名裸标识符（实测：新增 tools/长期优化快照.py 的
    # 模块级 `snapshot()`）就把 `('agent/knowledge_index.py','snapshot')`
    # 这条基线判成「已接线」→ 假红，且会诱导人删基线（= 把真死代码洗白）。
    # 属性访问（attrs，含属性调用）与字符串引用（strs）保留——那才是
    # 方法能被调用的真实形态。函数闸门（_agent_public_functions）不受影响：
    # 模块级函数**就是**靠裸名调用的。
    _calls, attrs, strs = _references(srcs)
    out: list[tuple[str, int, str, str]] = []
    for f, lineno, cls, name in _class_public_methods(srcs, include_private=include_private):
        if (name, f) in _REG or (f, name) in ALLOWLIST:
            continue
        if attrs.get(name) or strs.get(name):
            continue
        out.append((f, lineno, cls, name))
    return out


def _class_upper_members(sources: dict | None = None):
    """(文件, 行号, 类名, 成员名)——agent/ 类内的**大写成员**（枚举成员 + 类级常量）。

    2026-10-14 台账 T3.3（Stage 3 · M5 剩余三项之一）。
    审计 RC1 的元根因是「零调用闸门只扫模块级函数」；P3 已把类**方法**扩进来
    （_class_public_methods，79 条基线），但**枚举/大写常量成员仍在网外**——
    实测 96 个成员、5 个零引用（T0.4 量化，覆盖率 94.8%）。

    口径（T0.4 收敛过的定义，不是「任意大写赋值」的粗口径）：
      - Enum 子类的成员；
      - 类体内的全大写 `NAME = ...` 赋值。
    两者都只取 **agent/ 的类**，不含模块级常量（模块级归 _agent_public_functions 那条线管）。
"""
    srcs = sources if sources is not None else _parsed
    for f in sorted(srcs):
        if not f.startswith("agent/"):
            continue
        for cls in ast.walk(srcs[f]):
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in cls.body:
                if not isinstance(node, ast.Assign):
                    continue
                for tgt in node.targets:
                    # isupper() 对 `_PRIVATE_CONST` 也返回 True（下划线不影响大小写判断），
                    # 所以必须显式排掉下划线前缀——公共/私有的划分与方法闸门保持一致
                    # （`_class_public_methods` 也是 `not name.startswith('_')`）。
                    if (isinstance(tgt, ast.Name) and tgt.id.isupper()
                            and not tgt.id.startswith('_')):
                        yield f, node.lineno, cls.name, tgt.id


def _zero_ref_class_upper_members(sources: dict | None = None) -> list[tuple[str, int, str, str]]:
    """真正零引用的类级大写成员（枚举成员 / 类常量）。

    证据模型与 _zero_ref_class_methods **一致**：成员必须经**属性**（`Class.MEMBER`、
    枚举的 `Class.MEMBER` / 实例 `.MEMBER`）或**字符串字面量**到达；
    **不吃裸名**——裸标识符 `FOO` 只可能指向模块级常量或局部变量，
    不可能指向 `SomeClass.FOO`（T2.3 的教训：吃裸名会对同名模块级符号假红）。

    不做基线剔除（同 _zero_ref_class_methods 的分工）。
"""
    srcs = sources if sources is not None else _parsed
    _calls, attrs, strs = _references(srcs)
    out: list[tuple[str, int, str, str]] = []
    for f, lineno, cls, name in _class_upper_members(srcs):
        if attrs.get(name) or strs.get(name):
            continue
        out.append((f, lineno, cls, name))
    return out

def _walk_with_parent(node, parent=None):
    yield node, parent
    for child in ast.iter_child_nodes(node):
        yield from _walk_with_parent(child, node)


def _target_names(node) -> list[str]:
    """赋值目标扁平化——will_interject, score, reason = ... 的目标是 Tuple，要展开"""
    names = []
    for tg in node.targets:
        if isinstance(tg, ast.Name):
            names.append(tg.id)
        elif isinstance(tg, ast.Attribute):
            names.append(tg.attr)
        elif isinstance(tg, (ast.Tuple, ast.List)):
            for el in tg.elts:
                if isinstance(el, ast.Name):
                    names.append(el.id)
                elif isinstance(el, ast.Attribute):
                    names.append(el.attr)
    return names


def _contains_call_attr(node, attr: str) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == attr:
            return True
    return False


HANDLER = "agent/handler.py"
AUTONOMY = "agent/handler_autonomy.py"


class TestZeroCallerGate:
    """A. 零调用扫描——新写 infra 不接业务 → 红灯"""

    def test_all_public_functions_are_wired(self):
        dead = []
        for f, lineno, name in _agent_public_functions():
            if (name, f) in _REG:
                continue  # 技能注册即接线
            refs = _CALLS.get(name, set()) | _ATTRS.get(name, set()) | _STRS.get(name, set())
            if not refs and (f, name) not in ALLOWLIST:
                dead.append(f"{f}:{lineno} :: {name}")
        assert not dead, (
            "接线审计（CLAUDE.md 教训#14）：以下 agent/ 公共函数无任何生产调用方。\n"
            "处理：接线（谁该调用它）或归档（删除，反模式#6）；\n"
            "确认是标准工具函数则在 tests/test_integration_contract.py 的 ALLOWLIST 登记理由。\n"
            + "\n".join(dead))

    def test_allowlist_entries_still_exist(self):
        """ALLOWLIST 条目过期即失效——函数删了，登记也必须删。

        2026-10-24：ALLOWLIST **同时服务两个扫描器**——`_zero_ref_class_methods`
        里有 `(f, name) in ALLOWLIST`（类方法），而这条存在性检查原先只看
        `_agent_public_functions()`（模块级）。于是登记在 ALLOWLIST 里的**类方法**
        （会客厅的 HTTP 框架回调 `do_GET/do_POST/do_DELETE/log_message`）会被
        误报「登记了不存在的函数」。`登记了但检查器看不见` 正是教训 #30 的同族
        （检查器本身在骗人），所以两边口径必须一致。
        """
        existing = {(f, n) for f, _, n in _agent_public_functions()}
        existing |= {(f, n) for f, _lineno, _cls, n in _class_public_methods()}
        stale = [f"{f} :: {n}" for (f, n) in ALLOWLIST if (f, n) not in existing]
        assert not stale, "ALLOWLIST 登记了不存在的函数——函数已归档，删除对应登记：\n" + "\n".join(stale)


class TestAliasTableScope:
    """F. 别名表作用域（2026-10-13 P3 扫描器修复）。

    背景：零调用闸门此前用 `_references()` 存键时套 `_canon`、查找时用裸名。
    而 `_global_aliases()` 走 ast.walk **整个文件**，把函数内部的局部赋值也收进
    全局别名表——实测 218 条 Name 目标里 210 条来自函数体（`clean = fname`、
    `energy = energy_val`…）。后果：`_canon("clean") == "fname"`，引用被存到别的键下，
    **真在被调用**的 `ReplyPipeline.clean` / `MoodEngine.energy` 被判成零调用（假红）。

    这是教训 #30/#31 的第三次同族：检查器本身在骗人。所以这组测试同时要有
    **阴性对照**（局部绑定不许进表）和**阳性对照**（真别名必须还在表里）——
    只测「没报错」等于没测。
    """

    @staticmethod
    def _aliases(src: str) -> dict[str, str]:
        return _global_aliases({"__probe__.py": ast.parse(src)})

    def test_alias_table_ignores_function_local_bindings(self):
        """阴性对照：函数体内 `clean = fname` 不是别名——这正是假红的根因。"""
        m = self._aliases(
            "def f():\n"
            "    clean = fname\n"
            "    energy = energy_val\n"
            "    return clean, energy\n"
        )
        assert "clean" not in m, (
            "函数内部局部赋值被当成全局别名——_canon 会改写真实函数名，"
            "零调用闸门将把真在被调用的方法判成死代码（实测假红 2 个）：%r" % m)
        assert "energy" not in m, "同上：局部 energy 绑定进了全局别名表"

    def test_alias_table_still_registers_real_bindings(self):
        """阳性对照：三条真实别名路径一条都不能丢，否则是另一种失效。"""
        m = self._aliases(
            "from mod import real_fn as alias1\n"
            "module_alias = real_fn\n"
            "class C:\n"
            "    def __init__(self, fn):\n"
            "        self._slot = fn\n"
        )
        assert m.get("alias1") == "real_fn", "import ... as 别名丢了"
        assert m.get("module_alias") == "real_fn", "模块级 target = func 绑定丢了"
        assert m.get("_slot") == "fn", (
            "self.x = y 绑定丢了——这是别名表存在的理由（self._dispatch = dispatch）")

    def test_declared_name_stays_findable_despite_alias(self):
        """回归钉：别名存在时声明名仍必须查得到（实测两个假红样本）。

        别名表**做不到**干净——它无法区分「self.x = 函数」与「self.x = 数据字段」，
        实测 agent/mood.py:164 的 `self._state.energy = energy_val` 就把公开名
        `energy` 改写了。所以修法不是「让别名表变干净」，而是**存引用时同时
        登记原始名**（_record）。
        """
        srcs = {
            "a.py": ast.parse(
                "class C:\n"
                "    def __init__(self, v):\n"
                "        self.energy = v\n"),
            "b.py": ast.parse(
                "def f(engine):\n"
                "    return engine.energy()\n"),
        }
        calls, attrs, strs = _references(srcs)
        assert calls.get("energy") or attrs.get("energy"), (
            "声明名 energy 查不到引用——引用被别名改写后存到了别的键下，"
            "零调用闸门会把它判成死代码（实测假红）")


class TestActionVocabularyGate:
    """E. 命名动作词表穷举 + 阳性对照（2026-10-13 P1）。

    背景：审计 P0-1 实测 `Drives.release_by_action` 的 mapping 里
    `reply_commitment` 在生产代码**零调用**——表里写了、行为没接上，
    属于「写了没生效」家族（教训 #12/#14/#30）。同一个语义的数值还散成两处
    写死的 `release("commitment", 0.2)`，改表不改那两处就是下一次分叉。

    为什么闸门不能把名字写死在测试里：写死就会漂移（新增一个动作名，
    测试照样绿）。所以：
      ① 从 drives.py 的 mapping **解析出**全部键；
      ② 扫生产代码里真实的 `release_by_action("<name>")` 调用点；
      ③ 每个键都必须有调用点。
    再配**阳性对照**：把调用点种进合成源码，解析器必须看得见——否则这只是
    一条「永远绿」的假闸门（教训 #30 的假阴同族）。
    """

    @staticmethod
    def _mapping_keys() -> set[str]:
        src = (BASE / "agent" / "drives.py").read_text(encoding="utf-8")
        keys: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "release_by_action"):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Dict):
                    for k in sub.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
        return keys

    @staticmethod
    def _call_sites(sources: dict[str, str]) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for f, src in sources.items():
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "release_by_action"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    found.setdefault(node.args[0].value, set()).add(f)
        return found

    def test_every_action_name_has_a_production_call_site(self):
        keys = self._mapping_keys()
        assert keys, (
            "没从 drives.release_by_action 解析出任何键——解析器失效，"
            "这条闸门已经变成摆设（先修解析，别放过）")
        sources = {
            f: (BASE / f).read_text(encoding="utf-8")
            for f in sorted(_parsed) if f.startswith("agent/")
        }
        calls = self._call_sites(sources)
        missing = sorted(k for k in keys if not calls.get(k))
        assert not missing, (
            "以下命名动作在 agent/ 生产代码里零调用——表里写了、行为没接上"
            "（审计 P0-1 的 `reply_commitment` 就是这一族）：\n"
            + "\n".join(f"  {k}" for k in missing)
            + "\n处理：接线，或从 mapping 里删掉（不要留着当文档）。")

    def test_checker_sees_a_planted_call_site(self):
        """阳性对照：种一个调用点必须被看见——只测「跑起来没报错」等于没测。"""
        sources = {"agent/__planted__.py": (
            "def f(self, drives):\n"
            "    drives.release_by_action(\"__planted_probe__\")\n"
        )}
        assert self._call_sites(sources).get("__planted_probe__") == {
            "agent/__planted__.py",
        }

    def test_settlement_amount_has_a_single_source(self):
        """结算的释放量只许有一个来源（drives 的 mapping）。

        改造前 handler.py 与 self_state.py 各写死一处 `release("commitment", 0.2)`
        ——改表不改那两处就是静默分叉。这里把「写死的承诺释放量」钉成红灯。
        """
        offenders = []
        for f in sorted(_parsed):
            if not f.startswith("agent/"):
                continue
            src = (BASE / f).read_text(encoding="utf-8")
            for lineno in self._hardcoded_commitment_releases(src):
                offenders.append(f"{f}:{lineno}")
        assert not offenders, (
            "出现写死的承诺压力释放量——数值唯一来源是 "
            "drives.release_by_action 的 mapping：\n" + "\n".join(offenders))

    @staticmethod
    def _hardcoded_commitment_releases(src: str) -> list[int]:
        """AST：真实写死的 `...release("commitment", <数字>)` 调用的行号。

        **必须查 AST 不能查文本**——第一版用的是正则
        `release\\(\\s*["']commitment["']\\s*,`，结果被**我自己的说明注释**
        误报（注释里引用了那行旧代码）。这正是交接文档 E3 / 教训 #30 的同族问题：
        检查器本身也会骗人。AST 只看代码树，注释不进树。
        """
        hits: list[int] = []
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return hits
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "release"
                    and len(node.args) >= 2):
                continue
            first, second = node.args[0], node.args[1]
            if (isinstance(first, ast.Constant) and first.value == "commitment"
                    and isinstance(second, ast.Constant)
                    and isinstance(second.value, (int, float))
                    and not isinstance(second.value, bool)):
                hits.append(node.lineno)
        return hits

    def test_hardcoded_release_checker_sees_a_planted_call(self):
        """阳性对照：种一行真的写死调用，检查器必须报出来。

        没有这一条，上面那条闸门可能从上线起就恒绿（教训 #30：安全检查必须做
        阳性对照——种一个真样本进去，看它报不报）。
        同时做**阴性对照**：注释里的同一行文本不算（文本匹配的镜像错误）。
        """
        planted = (
            "def f(self, drives):\n"
            "    drives.release(\"commitment\", 0.2)\n"
        )
        assert self._hardcoded_commitment_releases(planted) == [2]

        comment_only = (
            "def f(self, drives):\n"
            "    # 曾经是 drives.release(\"commitment\", 0.2)\n"
            "    return 1\n"
        )
        assert self._hardcoded_commitment_releases(comment_only) == [], (
            "注释里的旧代码被当成真调用——检查器又退回文本匹配了")


# (函数/方法名, 必须被调用的文件列表, 为什么这条契约存在)
PIPELINE_CONTRACTS = [
    ("get_drive_context", ["agent/context_builder.py"],
     "驱动力投影必须经 context_builder 模板注入 system prompt（2026-08-15 整体审查："
     "handler 直追加会与模板双注入——context_builder 是唯一注入点）"),
    ("get_recent_dialogue", ["agent/handler_autonomy.py"],
     "私聊主动说话必须有最近对话上下文（2026-08-15 补全）"),
    ("evaluate", [HANDLER],
     "插话评分必须接在群消息处理路径"),
    ("get_available_styles_for_prompt", [HANDLER],
     "情绪标签列表必须进入 LLM 工具描述（2026-08-15 接线，此前零调用）"),
    # 2026-08-16 范式转换（教训 #24）：parse_natural_schedule 已删除——
    # 自然语言定时意图一律 LLM 工具；此契约条目随删除移除
    ("get_method_type", [HANDLER],
     "工具方法类型查询必须接入（经 import 别名 _skill_method_type）"),
    ("record_interjection", [HANDLER],
     "插话命中必须记账（配额/冷却数据源）"),
    ("release_by_action", [HANDLER],
     "插话结果必须释放驱动力（initiated/avoided）"),
]


class TestPipelineContract:
    """B. 管线契约——关键入口点必须在指定文件被调用"""

    @pytest.mark.parametrize(
        "name,files,why", PIPELINE_CONTRACTS, ids=[c[0] for c in PIPELINE_CONTRACTS])
    def test_entry_point_wired(self, name, files, why):
        callers = _CALLS.get(name, set()) | _ATTRS.get(name, set()) | _STRS.get(name, set())
        hit = [f for f in files if f in callers]
        assert hit, f"管线契约破裂：{name} 不在 {files} 中被调用——{why}"


class TestVoiceEmotionTagContract:
    """D. 语音情绪标签约束——send_voice 工具描述必须含「最多一个+白名单」约束。
    （2026-08-24 批D：LLM 标白名单外词/标多个 → 全部兜底 normal，情绪表达失效；
    约束被删或改松 → 红灯）"""

    def test_send_voice_description_constrains_emotion_tag(self):
        # 批D 白名单约束（2026-08-24 深夜改版：主通道升级为必填枚举参数，
        # 文本标签协议保留给语音模式路径——白名单约束必须仍在描述里）
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert "只能用列表里的词" in src, (
            "send_voice 文本标签路径的白名单约束被删——语音模式下 LLM 会自创标签，"
            "白名单外词全部兜底 normal，情绪表达失效")

    def test_send_voice_description_clarifies_tag_vs_topic(self):
        # 2026-08-24 晚：LLM 把话题里的情绪误标成说话语气——
        # 「有人难过的时候」被标「难过」用 sad 声线，与温柔陪伴内容打架。
        # 语义澄清「语气≠话题情绪」必须留在描述里。
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert "语气不是话题情绪" in src, (
            "send_voice 工具描述的话题/语气语义澄清被删——"
            "LLM 会重新把话题里的情绪词（「有人难过的时候」）误标成语音情绪标签")

    def test_send_voice_requires_emotion_enum(self):
        # 2026-08-24 深夜：「不写用默认语气」给了 LLM 跳过情绪决策的许可——
        # 全部语音兜底 normal 音色「局促统一」（主人实测反馈）。emotion 升级为
        # 必填枚举参数：每条语音 LLM 都必须决策语气，且白名单枚举防自创词。
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert '"required": ["text", "emotion", "speed", "pause"]' in src, (
            "send_voice 的 text/emotion/speed/pause 不再是必填——LLM 会跳过内容或韵律决策，"
            "语音重新退化为统一的默认语速或默认停顿")
        assert 'sorted(EMOTION_SPEED.keys())' in src, (
            "send_voice 的 emotion 枚举不再来自 EMOTION_SPEED 单一来源——"
            "枚举与音色映射表可能分叉")

    def test_send_voice_carries_exact_spoken_text(self):
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert "语音里只说 text 的内容" in src, (
            "send_voice 没有明确区分语音正文与工具后的自然语言回复——"
            "用户要求只说一句时，LLM 容易擅自扩写")
        assert src.count('turn_actions.get("voice_text") or reply') >= 2, (
            "LLM 明确选择的语音正文没有同时接到群聊和私聊发送路径")

    def test_voice_success_log_does_not_claim_planned_gpt_speaker(self):
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert '语音已发送 ({voice_desc})' not in src, (
            "发送成功日志仍把计划的 GPT-SoVITS 音色当成实际音色；"
            "降级 Edge-TTS 时会产生误导性取证记录")

    def test_voice_prosody_reaches_group_and_private_send_paths(self):
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert src.count('speed=turn_actions.get("voice_speed", 1.0)') >= 2, (
            "LLM 选择的语速没有同时接到群聊和私聊发送路径")
        assert src.count('pause=turn_actions.get("voice_pause", "自然")') >= 2, (
            "LLM 选择的停顿风格没有同时接到群聊和私聊发送路径")

    def test_voice_emotion_chain_has_no_keyword_fallback(self):
        # 2026-08-24 晚：真凶取证——「有人难过的时候」被 classify_emotions
        # 子串匹配标「难过」用 sad 声线（日志 97→97字 = LLM 未标签，关键词层
        # 抢答）。话题情绪 vs 说话语气是语义区分，关键词工程修不好——
        # 决策链只能是 LLM 标签 → 显式参数 → mood → normal。
        # 用 AST 查真实代码（注释/字符串不算），防「好心修复」加回。
        src = Path(HANDLER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        hits = [
            (cls.name, m) for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef)
            for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            and m.name == "_send_voice_reply"
        ]
        assert hits, "找不到 _send_voice_reply 方法"
        for _, method in hits:
            refs = [
                n.id for n in ast.walk(method)
                if isinstance(n, ast.Name) and n.id == "classify_emotions"
            ]
            assert refs == [], (
                "语音情绪决策链重新出现关键词兜底（classify_emotions 子串匹配）——"
                "话题里的情绪词会被误标成说话语气，sad 声线配温柔内容（2026-08-24 事故复现）")


class TestSingleSourceThreshold:
    """C. 判定单一来源——插话判定只允许一处"""

    def test_will_interject_only_from_evaluate_or_yield(self):
        """will_interject 赋值只能来自 evaluate() 或退让约束（should_yield）——
        不允许第三判定点（2026-08-15 曾抓出 80/65 两处 evaluate 外重判）"""
        t = _parsed[HANDLER]
        violations = []
        saw_evaluate = False
        for node, parent in _walk_with_parent(t):
            if not isinstance(node, ast.Assign):
                continue
            names = _target_names(node)
            if "will_interject" not in names:
                continue
            # 来源 1：evaluate() 调用（判定唯一来源）
            if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "evaluate"):
                saw_evaluate = True
                continue
            # 来源 2：退让约束——if should_yield(...): will_interject = False（系统约束，非判定）
            if (isinstance(node.value, ast.Constant) and node.value.value is False
                    and isinstance(parent, ast.If) and _contains_call_attr(parent.test, "should_yield")):
                continue
            violations.append(node.lineno)
        assert saw_evaluate, "handler.py 的插话判定没有接 evaluate()——管线断了"
        assert not violations, (
            f"handler.py 出现 evaluate 外的 will_interject 赋值（第二判定点）: 行 {violations}\n"
            "门槛必须通过 threshold_override 传入 evaluate——判定与日志只能有一处")


class TestConversationDecisionContract:
    """群聊窗口只负责把消息交给 LLM，是否开口由 LLM 单一决策。"""

    @staticmethod
    def _group_handler_source() -> str:
        src = Path(HANDLER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_group_message":
                return ast.get_source_segment(src, node) or ""
        raise AssertionError("找不到 handle_group_message")

    def test_active_window_has_no_system_content_gate(self):
        """窗口内不再按等待状态、长度、标点或连续次数拦截消息。"""
        method = self._group_handler_source()
        window = method.split("# 🆕 对话窗口连续性", 1)[1].split("# 插话引擎", 1)[0]
        assert "should_reply = True" in window
        assert "is_waiting_for_reply" not in window
        assert "len(text.strip())" not in window
        assert "should_yield" not in window
        assert "has_other_at" not in window

    def test_active_window_bypasses_group_batch_merge(self):
        """窗口消息逐条交给 LLM，不被 2 秒批处理合并成另一条消息。"""
        method = self._group_handler_source()
        batching = method.split("# 🆕 R1-6: 防抖/批处理", 1)[1].split("# 🍬 自主节律", 1)[0]
        assert "is_engaged(user_id, group_id)" in batching
        assert 'not msg.get("_is_pending")' in batching

    def test_routed_turn_has_no_random_lazy_veto(self):
        """进入回复管线后不能再由系统随机决定“懒得回”。"""
        method = self._group_handler_source()
        routed = method.split("if not should_reply:", 1)[1].split("# 打字节奏", 1)[0]
        assert "lazy_chance" not in routed
        assert "random.random()" not in routed

    def test_voice_only_paths_do_not_return_before_common_finalizer(self):
        """群聊和私聊语音发送后都必须继续执行聊天记录/窗口状态收尾。"""
        src = Path(HANDLER).read_text(encoding="utf-8")
        group = self._group_handler_source()
        group_voice = group.split("# 🎤 语音发送", 1)[1].split("# 引用回复", 1)[0]
        assert "return" not in group_voice
        assert "self._busy = False" not in group_voice

        tree = ast.parse(src)
        private = next(
            ast.get_source_segment(src, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_private_message"
        )
        private_voice = private.split("# 🎤 语音发送", 1)[1].split("# 私聊中移除", 1)[0]
        assert "return" not in private_voice
        assert "self._busy = False" not in private_voice

    def test_private_text_path_initializes_self_memory_action_id(self):
        """纯文字私聊不能在自我记忆收尾引用未赋值的语音 action id。"""
        src = Path(HANDLER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        private = next(
            ast.get_source_segment(src, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_private_message"
        )
        voice = private.split("# 🎤 语音发送", 1)[1].split("# 私聊中移除", 1)[0]
        assert '_self_memory_action_id = ""' in voice

    def test_group_persistent_voice_mode_never_sends_text_twice(self):
        """语音路径自身已负责文字降级；群持久语音模式不得再走普通文字发送。"""
        group = self._group_handler_source()
        voice = group.split("# 🎤 语音发送", 1)[1].split("# 引用回复", 1)[0]
        after_send = voice.split("ok = await self._send_voice_reply", 1)[1]
        assert "if not _in_voice_mode:" not in after_send
        assert "_voice_only = True" in after_send

    def test_autonomy_send_paths_must_enrich(self):
        """handler_autonomy.py 里每个含 _checked_send 的函数必须先调 _enrich_reply
        且发送后调 log_chat——2026-08-16 抓出主动路径直发 LLM 原文（[贴图:开心]
        原样泄露）且主动说过的话不进聊天史（记忆提取看不到=失忆感）。
        正常回复路径（handler.py）enrich+log_chat 后发，主动路径漏了——此闸门钉住。"""
        t = _parsed[AUTONOMY]
        assert t is not None, "handler_autonomy.py 解析失败"
        for node in ast.walk(t):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = [n.func for n in ast.walk(node) if isinstance(n, ast.Call)]
                names = {getattr(c, "id", getattr(c, "attr", "")) for c in calls}
                attributes = {
                    n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
                }
                if "_checked_send" in names:
                    assert "_enrich_reply" in names, (
                        f"{node.name} 直接 _checked_send 但未调 _enrich_reply——"
                        f"[贴图:xx]/<思考> 会原样泄露（2026-08-16 事故）")
                    # 自治路径的聊天史回写经过取消安全 Store helper，
                    # 因而 log_chat 作为函数对象出现在 Attribute 而非 Call.func。
                    assert "log_chat" in names or "log_chat" in attributes, (
                        f"{node.name} 主动发送但未调 log_chat——"
                        f"糖糖主动说的话不进聊天史（2026-08-16 流程审计）")

    # 2026-08-16 发送链路全审计：LLM 文本直发点清单——每个函数必须调 _enrich_reply。
    # 新增加 LLM 直发路径时追加到本表（否则闸门不知道它的存在）。
    LLM_SEND_FUNCS = [
        (HANDLER, "handle_group_increase"),    # 入群欢迎
        (HANDLER, "handle_friend_request"),    # 好友申请打招呼
        (HANDLER, "handle_poke"),              # T2.7：被戳回应改由 LLM 生成
        (HANDLER, "_execute_natural_action"),  # pm/群发言 LLM 生成
        ("agent/handler_commands.py", "_generate_and_send"),  # /说
        # /传话现在走统一 send_actions helper，非 LLM 生成路径；
        # 其清洗/回执契约由 tests/test_send_message.py 覆盖。
    ]

    @pytest.mark.parametrize("file,func", LLM_SEND_FUNCS)
    def test_llm_send_paths_must_enrich(self, file, func):
        """LLM 生成的文本发往用户前必须过 _enrich_reply（清洗+贴图解析）——
        2026-08-16 全链路审计抓出 6 处直发点（欢迎/打招呼/延迟发送/遥控发言），
        [贴图:xx] 标签原样泄露。此表钉住每一个已知 LLM 直发函数。"""
        t = _parsed[file]
        assert t is not None, f"{file} 解析失败"
        for node in ast.walk(t):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
                calls = [n.func for n in ast.walk(node) if isinstance(n, ast.Call)]
                names = {getattr(c, "id", getattr(c, "attr", "")) for c in calls}
                assert names & {"_enrich_reply", "_enrich_reply_async"}, (
                    f"{file}:{func} 发送 LLM 文本但未调同步/异步 enrich——"
                    f"标签/思考段会原样泄露给用户")
                break
        else:
            raise AssertionError(f"{file} 中找不到函数 {func}（改名后请同步本表）")

    def test_sing_markers_are_captured_before_group_and_private_reply_cleaning(self):
        """[SING:] 是媒体动作，不得在 ReplyPipeline 清洗后才解析。"""
        src = Path(HANDLER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for name in ("handle_group_message", "handle_private_message"):
            method = next(
                ast.get_source_segment(src, node) or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == name
            )
            marker_parse = method.index("self._parse_sing_tag(sing_marker_reply)")
            # 2026-09-22：[分段] 需要「先拆条再逐段 enrich」，私聊的整条 enrich
            # 换成了 _enrich_split_reply——两条路都在 clean 之前，本闸门判的是
            # 「[SING:] 解析早于 enrich」这个契约，不是某一种 enrich 的写法。
            enrich_at = [i for i in (
                method.find("await self._enrich_split_reply(reply"),
                method.find("await self._enrich_reply_async(reply"),
            ) if i != -1]
            assert enrich_at, f"{name} 找不到任何 enrich 调用"
            assert marker_parse < min(enrich_at), f"{name} 在清洗后才解析 [SING:]"
            assert "await self._send_singing_actions(" in method, (
                f"{name} 未将唱歌交给 ActionPlan child receipt"
            )
            assert "await self._send_singing_action(" not in method
            assert "await self._send_singing_reply(" not in method

    def _method_src(self, path: str, name: str) -> str:
        src = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(src, node) or ""
        raise AssertionError(f"{path} 中找不到方法 {name}（改名后请同步本闸门）")

    def test_split_markers_are_captured_before_private_reply_cleaning(self):
        """[分段] 是发送协议，不得在 ReplyPipeline 清洗后才解析（2026-09-22）。

        2026-09-22 调研：AstrBot 有过一模一样的需求（issue #338，标记 `|分段|`），
        当场撞上标记泄漏「你好 |分段|很高兴见到你」，最后没采纳这条路线。
        所以这里钉死三件事——它们合起来才等于「接线是活的」：
          ① 私聊走 _enrich_split_reply（拆条 + 逐段 enrich），而不是先整条 enrich；
          ② 私聊发送点把段列表交给 _checked_send；
          ③ 拆条助手自己先 parse 再 enrich。
        阳性对照（2026-09-22 实跑）：删掉 ② 那一行 → 本闸门变红。
        """
        method = self._method_src(HANDLER, "handle_private_message")
        assert "await self._enrich_split_reply(reply" in method, (
            "handle_private_message 未走 _enrich_split_reply——[分段] 分不出来")
        assert "presplit_parts=send_parts" in method, (
            "私聊发送点没把段列表交给 _checked_send——拆了也只会发一条（教训 #30）")
        assert "await self._enrich_reply_async(reply" not in method, (
            "handle_private_message 里还有一条「整条 enrich」路径——"
            "它会在拆分之前把 [分段] 剥掉，拆条静默失效")
        assert "await self._enrich_split_reply(reply" in method

    def test_enrich_split_reply_parses_before_enriching(self):
        """拆条助手自己的顺序：parse → 逐段 enrich。顺序反了标记就没了。"""
        fn = self._method_src(HANDLER, "_enrich_split_reply")
        parse_at = fn.index("parse_split_markers(reply)")
        enrich_at = fn.index("await self._enrich_reply_async(")
        assert parse_at < enrich_at, (
            "_enrich_split_reply 先 enrich 后 parse——clean() 已把标记剥掉")

    def test_split_marker_strip_is_wired_in_clean(self):
        """clean() 必须兜底剥掉 [分段]（**只剥不拆**）。

        主动插话 / 群聊 / 命令这些路径不分段，标记一旦活到那里就会当众穿帮
        （AstrBot #338 的现场）。少拆一条不影响对话，漏出标记会。
        """
        clean_src = self._method_src(
            str(BASE / "agent" / "reply_pipeline.py"), "clean")
        # 2026-10-12 M2：clean() 的兜底改成统一出口 strip_protocol_markers()——
        # 正因一条条列，上一轮漏了 [不说话]（审计 P2 grep 0 命中）。
        # 断言两个分支都走统一出口，等于同时钉住"分段/引用/不说话"三者的兜底。
        assert clean_src.count("strip_protocol_markers(") >= 2, (
            "clean() 的主路径或回退分支漏了协议标记统一兜底——"
            "回退分支会把刚剥掉的协议标记重新带回来")
        assert clean_src.count("strip_split_markers(") == 0, (
            "clean() 里还有零散的标记剥离——协议标记必须只从 "
            "protocols.strip_protocol_markers 一处剥（新增标记才不会再漏）")

    def test_split_protocol_is_injected_in_private_only(self):
        """分条协议只注入私聊，群聊暂时不注入（2026-09-22 的显式边界）。

        群聊那侧有两个未答的问题：53% 的回复带 [CQ:reply] 引用前缀（挂哪一段？）、
        以及群里有第三人围观多条气泡的观感。所以群聊要等 M3，**不是漏了**——
        这条闸门就是防止有人「顺手补齐」把它挪过去。
        反过来也钉住：协议文本被删掉就等于功能静默失效（教训 #30 同族）。
        """
        private = self._method_src(HANDLER, "handle_private_message")
        group = self._method_src(HANDLER, "handle_group_message")

        assert "_protocols.SPLIT_PROTOCOL" in private, (
            "私聊没注入分条协议——糖糖不知道 [分段] 存在，整个功能是死的")
        assert "_protocols.SPLIT_PROTOCOL" not in group, (
            "分条协议被注入到群聊了——引用前缀挂哪一段还没有答案（M3 再开）")

    # ═══════════════════════════════════════════════════════════════
    # 引用判断归 LLM（2026-09-23）
    #
    # 主人定案：「挂载需要由 LLM 和系统共同决定——系统负责轮次，LLM 需要能自主
    # 决定是否需要引用。」旧规则（is_at_bot → 无条件引用）让群聊 53.2% 的回复
    # 都带引用前缀（实测 4836/9086），而且是系统替 LLM 做决策（教训 #9/#24）。
    # ═══════════════════════════════════════════════════════════════

    def test_quote_marker_is_read_before_group_reply_cleaning(self):
        """[引用] 必须在 clean 之前读——clean 会剥掉行首 1–5 字的方括号标签，
        标记一到那里就没了，于是永远读不到「她想引用」。

        阳性对照（2026-09-23 实跑）：把 `_wants_quote = ...` 挪到 enrich 之后，
        本闸门变红。
        """
        group = self._method_src(HANDLER, "handle_group_message")
        parse_at = group.index("has_quote_marker(reply)")
        enrich_at = [i for i in (
            group.find("await self._enrich_split_reply(reply"),
            group.find("await self._enrich_reply_async(reply"),
        ) if i != -1]
        assert enrich_at, "handle_group_message 找不到 enrich 调用"
        assert parse_at < min(enrich_at), (
            "[引用] 在清洗之后才读——clean() 已经把标记剥掉，永远判成「不引用」")

    def test_quote_decision_actually_gates_the_prefix(self):
        """读到的结果必须真的去门控 [CQ:reply] 前缀，否则就是"读了没用"（教训 #30）。"""
        group = self._method_src(HANDLER, "handle_group_message")
        assert "[CQ:reply,id={msg_id}]" in group, (
            "引用前缀的拼接点没了——她写了 [引用] 也不会挂")
        assert "_wants_quote" in group, "读到的引用意图没被用上"
        # 前缀那一段必须由 _wants_quote 把关
        idx = group.index("[CQ:reply,id={msg_id}]")
        window = group[max(0, idx - 260):idx]
        assert "_wants_quote" in window, (
            "[CQ:reply] 前缀没有由 _wants_quote 把关——又变成无条件引用了")

    def test_quote_protocol_is_injected_in_group_only(self):
        """引用协议只注入群聊：引用前缀目前只加在群聊路径上，
        在私聊注入「你可以引用」就是一句假话。"""
        assert "_protocols.QUOTE_PROTOCOL" in self._method_src(
            HANDLER, "handle_group_message"), "群聊没注入引用协议——她不知道能引用"
        assert "_protocols.QUOTE_PROTOCOL" not in self._method_src(
            HANDLER, "handle_private_message"), "私聊注入了引用协议，但私聊不挂引用"

    # ═══════════════════════════════════════════════════════════════
    # 情绪自报 [心情:词]（2026-10-13 M3 剩余项，审计 P0-2）
    #
    # 审计实测：五个正向事件（被夸/被感谢/被想念/亲密聊天/帮到别人）一个发射点
    # 都没有——心情只会被 _apply_decay 收敛回中性 70。修法是「回复 LLM 自报标签
    # 回灌」。下面三条钉住这条路线的三个必要条件：读得早、注得有闸门、回灌在送达后。
    # ═══════════════════════════════════════════════════════════════

    def test_mood_report_is_read_before_cleaning_in_both_paths(self):
        """[心情:X] 必须在 clean 之前读——clean() 会把方括号内容剥掉。"""
        for method, tag in (("handle_group_message", "群聊"),
                            ("handle_private_message", "私聊")):
            body = self._method_src(HANDLER, method)
            parse_at = body.index("parse_mood_report(reply)")
            enrich_at = [i for i in (
                body.find("await self._enrich_split_reply(reply"),
                body.find("await self._enrich_reply_async(reply"),
            ) if i != -1]
            assert enrich_at, f"{tag} 找不到 enrich 调用"
            assert parse_at < min(enrich_at), (
                f"{tag}：{tag}的 [心情:X] 在清洗之后才读——clean() 已剥掉标记，"
                "永远判成「没自报」")

    def test_mood_report_protocol_is_conditional_not_resident(self):
        """审计「不要动」第 11 条：**不加无条件常驻注入**（2026-08-17 同形注入
        被实测有害）。所以协议必须挂在 `_mood_report_turn` 这个闸门后面——
        它以「对方刚回了她上一条」为条件，不是每轮都注。

        这条闸门防两件事：① 有人把它改成常驻；② 有人「顺手」把它加进
        `minimal` 路径（遥控/播报那种非对话路径根本没有交流发生）。
        """
        for method in ("handle_group_message", "handle_private_message"):
            body = self._method_src(HANDLER, method)
            idx = body.index("_protocols.MOOD_REPORT_PROTOCOL")
            window = body[max(0, idx - 260):idx]
            assert "_mood_report_turn" in window, (
                "MOOD_REPORT_PROTOCOL 没有挂在 _mood_report_turn 闸门后面——"
                "变成无条件常驻注入了（审计「不要动」第 11 条）")
        # 闸门本身必须由 feedback 候选（= 对方刚回了她）驱动
        for method in ("handle_group_message", "handle_private_message"):
            body = self._method_src(HANDLER, method)
            assert "_mood_report_turn = bool(feedback_candidate)" in body, (
                "闸门不再是「对方刚回她上一条」——条件被改宽了")

    def test_mood_report_is_backfilled_only_after_confirmed_delivery(self):
        """回灌必须在**确认送达之后**——没发出去的话不算一次真实交流
        （与 SENT_REPLY / 自我记忆同一口径）。

        断言方式：`_apply_mood_report(_mood_report, ...)` 的出现位置必须在
        `if ok:` 块内。用「最近的那个缩进块」判断，不猜行号。

        2026-10-13 T1.2：调用点加了 `injected=` / `source=` 两个**只读观测**参数，
        故断言从 `(_mood_report)`（连右括号）放宽为 `(_mood_report`（第一实参不变）。
        闸门意图不变：读了自报就必须回灌，且必须在确认送达之后。
        """
        for method in ("handle_group_message", "handle_private_message"):
            body = self._method_src(HANDLER, method)
            assert "_apply_mood_report(_mood_report" in body, (
                f"{method} 读了自报却从不回灌——读了没用（教训 #30）")
            idx = body.index("_apply_mood_report(_mood_report")
            # 往前找最近的 `if ok:`，中间不能出现同级 `if ` 之外的分支切断
            preceding = body[:idx]
            last_if = preceding.rfind("if ok:")
            assert last_if != -1 and idx - last_if < 1200, (
                f"{method}：回灌不在 `if ok:` 块里——发送失败也会污染她的情绪")

    def test_mood_report_injection_is_logged(self):
        """2026-10-13 T1.2：协议注入必须留痕——它是 S3 遵从率的分母。

        「闸门没开」与「注入了但她没写」在修复前完全同形（前者没有任何日志，
        后者被静默 return False）。这条闸门钉两件事：① 两条回复路径都在闸门内
        记了一行；② 该行不会被顺手删掉（删了遵从率就再也没法算）。
        """
        for method in ("handle_group_message", "handle_private_message"):
            body = self._method_src(HANDLER, method)
            assert "心情自报协议已注入" in body, (
                f"{method} 注入了心情自报协议却不留痕——遵从率的分母丢了")
            idx = body.index("心情自报协议已注入")
            window = body[max(0, idx - 400):idx]
            assert "_mood_report_turn" in window, (
                f"{method}：注入日志不在 _mood_report_turn 闸门内——会退化成常驻噪声")

    def test_no_keyword_emotion_detection_came_back(self):
        """情绪不做关键词检测（审计「不要动」＋ mood.py 的退役是决定不是遗漏）。

        自报路线全部靠 LLM 自报 + 白名单；一旦有人在 handler 里加回
        「文本里出现『谢谢』就给 BEING_THANKED」，这条闸门变红。
        """
        body = self._method_src(HANDLER, "_apply_mood_report")
        assert "in label" in body or "MOOD_REPORT_EVENTS.get" in body, (
            "_apply_mood_report 不再走白名单查表——自报路线被改成了别的判定")
        # 关键词表不该出现在这个方法里
        for kw in ("谢谢", "感谢", "想你", "夸奖", "谢谢"):
            assert f'"{kw}" in' not in body and f"'{kw}' in" not in body, (
                f"_apply_mood_report 里出现关键词判断（{kw}）——"
                "mood.py:220-221 的关键词检测退役是决定，不是遗漏")

    def test_system_quote_heuristic_does_not_come_back(self):
        """`should_quote` 已退役（2026-09-23）——它是系统替 LLM 判断该不该引用。

        这条闸门防的是「有人觉得群聊引用率掉了，顺手把旧规则加回来」。
        真要加回来，那是一次显式决定，得先删掉本闸门并说清理由。
        """
        assert "def should_quote" not in (
            BASE / "agent" / "reply_pipeline.py").read_text(encoding="utf-8"), (
            "should_quote 又被加回来了——引用判断的唯一决策源是 [引用] 标记")
        assert "def _should_quote" not in (
            BASE / "agent" / "handler.py").read_text(encoding="utf-8")

    PERSIST_STATE_KEYS = [
        "state:quiet_groups", "state:voice_blocked", "state:voice_mode",
        "state:care_due", "state:pending_pm", "state:poke_tracker",
        "state:like_tracker", "state:auto_pending", "state:auto_cold",
        "state:no_friend_until", "state:sed_active",
    ]

    def test_runtime_state_persistence_wired(self):
        """2026-08-16 主人规矩：这类功能都持久化——静默/开关/冷却/风控/
        草稿重启不丢（主动私聊冷却重启骚扰事故的教训推广）。
        闸门：每个 key 必须有「绑定形态」的恢复行
        `self._X = self._load_state_kv("state:key", ...)`——仅出现 key 字符串
        不够（写穿行也含 key；2026-08-16 Codex I5：_like_tracker 恢复行存在
        但被 __init__ 后置初始化覆盖成空，旧闸门全绿没拦住）。"""
        import re as _re_gate
        text = (BASE / "agent" / "handler.py").read_text(encoding="utf-8")
        for k in self.PERSIST_STATE_KEYS:
            # 三种合法形态：self._X = [set(]self._load_state_kv(...)（直接绑定）、
            # napcat 属性恢复（no_friend_until 住在 napcat 层）、
            # 两行绑定（2026-08-17 sed_active：raw 读取行 + 类型转换绑定行）
            m = _re_gate.search(
                rf'self\._(\w+)\s*=\s*(?:set\(|)?self\._load_state_kv\("{k}"', text)
            napcat_m = _re_gate.search(
                rf'self\._load_state_kv\("{k}".*?self\.napcat\._(\w+)\s*=', text, _re_gate.S)
            two_line_m = _re_gate.search(
                rf'_(\w+)\s*=\s*self\._load_state_kv\("{k}"[^\n]*\n\s*self\._(\w+)', text)
            assert m or napcat_m or two_line_m, f"状态 {k} 无绑定形态恢复行（self._X = self._load_state_kv）——重启会失忆"

    def test_opinion_invite_contract(self):
        """2026-08-16 Codex：征集邀请文案契约——
        1. LLM 邀请发送前必须过 _enrich（贴图/思考段不得字面泄露）
        2. 邀请 prompt 必须含「禁止提到主人」——征集是糖糖自己想问"""
        text = (BASE / "agent" / "opinion.py").read_text(encoding="utf-8")
        assert "self._enrich(reply" in text, "邀请文案未过 enrich——标签会泄露"
        assert "禁止提到「主人让我来」" in text, "邀请 prompt 丢失主人保密契约"
        # 导出目录必须在知识库扫描范围外（意见含私聊内容与 QQ 号）
        assert 'Path("data") / "意见收集"' in text or "data/意见收集" in text, (
            "意见文档导出目录落入知识库范围——群友可经 search_knowledge 检索")

    def test_state_kv_round_trip(self, tmp_path):
        """_save_state_kv/_load_state_kv 往返——JSON 序列化不丢类型"""
        from agent.handler import MessageHandler
        from agent.store import Store
        h = object.__new__(MessageHandler)
        h.memory = type("M", (), {})()
        h.memory.store = Store(str(tmp_path / "kv.db"))
        h._save_state_kv("state:test", {"a": [1, 2], "b": "x"})
        assert h._load_state_kv("state:test", None) == {"a": [1, 2], "b": "x"}
        assert h._load_state_kv("state:missing", {"d": 1}) == {"d": 1}

    def test_no_hardcoded_score_comparisons(self):
        """handler.py 不允许对插话 score 做整数比较——
        2026-08-15 抓出「threshold==65 and score>=65」渐变补判已移入 evaluate。
        （情绪 score 的浮点比较 0.35/0.65 是另一领域，不在此列）"""
        t = _parsed[HANDLER]
        bad = []
        for node in ast.walk(t):
            if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                    and node.left.id == "score"):
                int_consts = [c for c in node.comparators
                              if isinstance(c, ast.Constant) and isinstance(c.value, int)]
                if int_consts:
                    bad.append(node.lineno)
        assert not bad, f"handler.py 出现 score 整数比较（插话第二门槛）: 行 {bad}"




# ── 类级大写成员（枚举成员 / 类常量）零引用基线（2026-10-14 台账 T3.3）──
# 口径见 _class_upper_members 的 docstring（T0.4 收敛过：Enum 成员 + 类内大写赋值，
# 只算 agent/ 的类，不含模块级常量）。实测 95 个成员、5 个零引用（覆盖率 94.7%）。
#
# 这 5 条**不是新发现**——正是审计 P2 点名的 M5 遗留，T0.4 给了数量、T3.3 收口。
# 每条写明「为什么留」或「为什么现在删」，避免基线变成一句假话（教训 #35）。
#
# 与 CLASS_METHOD_BASELINE 同规则：**只许变小**。
CLASS_UPPER_MEMBER_BASELINE: set[tuple[str, str]] = {
    # ── 恒空/恒 None 的死壳（与 T2 的 那批 同类，但不在 T2 范围内）──
    # PersonalityEngine.TRIGGERS = {} + check_trigger() 恒返回 None。
    # 原注释：「特殊触发词（已禁用，让LLM自己发挥）」——禁用是**刻意的**：
    # 关键词触发回复正是教训 #9/#24 的反模式。
    # 保留理由：删 check_trigger 要连带删 handler.py:3596 的调用点与大段分支，
    # 属 T2 第二档「需确认再动」的范围，不在 T3.3（只扩闸门）里做。
    ("agent/personality.py", "TRIGGERS"),
    # 2026-10-23 T2.3：BUDGET_CONVERSATION 已删除（连同它唯一的消费者
    # _assemble_with_budget）。原白名单条目随之移除——基线只许变小。
    # ── MoodEvent 三个零发射成员（审计 P2 点名）──
    # mood.py:49-60 的注释已写明：正向事件留给「LLM 自报标签回灌」接线，
    # 而 IDLE_RECOVERY / REPEATED_QUESTIONS / TALKING_TOO_MUCH 不在那五条里。
    # 保留理由：它们是**能力定义**不是死代码——情绪模型的事件词汇表，
    # 缺发射点是「还没接」而不是「不该有」。删了会窄化可表达的情绪空间。
    ("agent/mood.py", "IDLE_RECOVERY"),
    ("agent/mood.py", "REPEATED_QUESTIONS"),
    ("agent/mood.py", "TALKING_TOO_MUCH"),
}


class TestEnumMemberGate:
    """H. 类级大写成员零调用闸门（2026-10-14 台账 T3.3，Stage 3 · M5 剩余三项之一）。

    补的是零调用闸门的**最后一块覆盖盲区**：`_agent_public_functions()` 扫模块级函数，
    `_class_public_methods()` 扫类公共方法（P3 扩的），但**枚举成员与类级大写常量**
    一直在网外。审计 RC1 的元根因（「闸门有覆盖率上限」）到这里才真正收口。

    三条测试分工（与 TestClassMethodGate 同构）：
      ① test_no_new_dead_upper_members —— 闸门本体（基线外新增 → 红）；
      ② test_upper_member_baseline_is_still_valid —— 防基线腐烂；
      ③ test_gate_sees_a_planted_dead_member —— **阳性对照**（种死成员必须红）；
      ④ test_bare_name_is_not_enum_member_evidence —— 阴性对照（同 T2.3 的教训）。
    """

    def test_no_new_dead_upper_members(self):
        found = {(f, n) for f, _, _, n in _zero_ref_class_upper_members()}
        new = sorted(found - CLASS_UPPER_MEMBER_BASELINE)
        assert not new, (
            "接线审计（教训#14）：以下 agent/ 类级大写成员（枚举成员/类常量）零引用，"
            "且不在基线上——\n"
            + "\n".join("  %s :: %s" % (f, n) for f, n in new)
            + "\n处理：接线（谁该发射它/读它）或归档；"
            "确认为待接线能力则登记进 CLASS_UPPER_MEMBER_BASELINE 并写明理由。"
            "\n**不要**为了变绿去扩大基线——基线只许变小。")

    def test_upper_member_baseline_is_still_valid(self):
        """基线只许变小：成员被删、或已被引用，都必须把条目删掉。"""
        existing = {(f, n) for f, _, _, n in _class_upper_members()}
        found = {(f, n) for f, _, _, n in _zero_ref_class_upper_members()}
        gone = sorted(k for k in CLASS_UPPER_MEMBER_BASELINE if k not in existing)
        assert not gone, (
            "基线登记了**已不存在**的成员——删除对应基线条目：\n"
            + "\n".join("  %s :: %s" % k for k in gone))
        wired = sorted(k for k in CLASS_UPPER_MEMBER_BASELINE
                       if k in existing and k not in found)
        assert not wired, (
            "基线登记了**已被引用**的成员——棘轮要收紧，删除对应基线条目：\n"
            + "\n".join("  %s :: %s" % k for k in wired)
            + "\n（条目留着不删，基线就会慢慢变成一句假话——教训 #35）")

    def test_gate_sees_a_planted_dead_member(self):
        """阳性对照（上游验收标准「故意种一个死枚举成员 → 闸门必须红」）。

        带三条阴性对照：被引用的成员不许误报、私有成员不进闸门、模块级常量不进闸门。
        """
        srcs = {
            "agent/__planted__.py": ast.parse(
                "from enum import Enum\n"
                "class E(Enum):\n"
                "    USED_ONE = 1\n"
                "    DEAD_ONE = 2\n"
                "\n"
                "class C:\n"
                "    LIVE_CONST = 10\n"
                "    DEAD_CONST = 20\n"
                "    _PRIVATE_CONST = 30\n"
                "\n"
                "def use():\n"
                "    return E.USED_ONE, C.LIVE_CONST\n"),
        }
        found = {(f, n) for f, _, _, n in _zero_ref_class_upper_members(srcs)}
        assert ("agent/__planted__.py", "DEAD_ONE") in found, (
            "种进去的死枚举成员没被看见——这条闸门是假的（教训 #30）")
        assert ("agent/__planted__.py", "DEAD_CONST") in found, (
            "种进去的死类常量没被看见——这条闸门是假的（教训 #30）")
        assert ("agent/__planted__.py", "USED_ONE") not in found, (
            "被 E.USED_ONE 引用的成员被误判成死代码")
        assert ("agent/__planted__.py", "LIVE_CONST") not in found, (
            "被 C.LIVE_CONST 引用的常量被误判成死代码")
        assert ("agent/__planted__.py", "_PRIVATE_CONST") not in found, (
            "私有成员不该进公共闸门")

    def test_bare_name_is_not_enum_member_evidence(self):
        """阴性对照（同 T2.3 的教训）：外来裸名不构成「成员被引用」的证据。

        裸标识符 `DEAD_ONE` 只可能指向模块级常量或局部变量，不可能指向 `E.DEAD_ONE`。
        若把裸名当证据，任何文件里出现同名标识符就会把真死成员洗白成「已接线」。
        """
        cls_src = chr(10).join([
            "from enum import Enum",
            "class E(Enum):",
            "    ONLY_ONE = 1",
        ])
        bare = {
            "agent/__planted__.py": ast.parse(cls_src),
            "tools/__planted_bare__.py": ast.parse("ONLY_ONE = 99"),
        }
        found = {(f, n) for f, _, _, n in _zero_ref_class_upper_members(bare)}
        assert ("agent/__planted__.py", "ONLY_ONE") in found, (
            "裸名 ONLY_ONE 被当成了成员证据——闸门会对同名模块级常量假红（T2.3）")

        attr_src = chr(10).join([
            "e = object()",
            "e.ONLY_ONE",
        ])
        wired = {
            "agent/__planted__.py": ast.parse(cls_src),
            "tools/__planted_attr__.py": ast.parse(attr_src),
        }
        found2 = {(f, n) for f, _, _, n in _zero_ref_class_upper_members(wired)}
        assert ("agent/__planted__.py", "ONLY_ONE") not in found2, (
            "属性访问 e.ONLY_ONE 没被认出来——闸门被改瞎了")

    def test_lowercase_class_attrs_are_not_members(self):
        """阴性对照（`isupper()` 的上界）：类内**小写**属性不是枚举成员/常量。

        现场：`agent/color_panel.py` 的 `colorChanged = Signal(...)` /
        `agent/ui_anim.py` 的 `transitionFinished = Signal(...)` 是 **Qt 信号对象**，
        不是常量。若把 `isupper()` 拿掉，这 5 个会混进成员集合
        （实测 60 → 65），闸门从此对 Qt 信号报零引用。

        这条测试是**变异探针逼出来的**：我先写的 M2 变异（放宽 isupper）**没让测试变红**，
        诊断后发现那 5 个新增成员恰好都不是零引用，所以闸门本体看不见——
        即「isupper 的上界没有任何测试覆盖」。补上这条才有。
        """
        srcs = {
            "agent/__planted__.py": ast.parse(
                "class C:\n"
                "    GOOD_CONST = 1\n"
                "    colorChanged = 2\n"
                "    _hidden = 3\n"),
        }
        members = {(f, n) for f, _, _, n in _class_upper_members(srcs)}
        assert ("agent/__planted__.py", "GOOD_CONST") in members
        assert ("agent/__planted__.py", "colorChanged") not in members, (
            "小写类属性（Qt 信号形态）混进了成员集合——isupper() 上界失守")
        assert ("agent/__planted__.py", "_hidden") not in members, (
            "私有属性混进了成员集合")

    def test_enum_member_count_is_sane(self):
        """自身校验：度量不是恒零——被引用的成员必须有（否则扫描器只会返回全集）。

        上游 T0.4 的量化口径：95 个成员 / 5 个零引用（94.8%）。
        本闸门加了 `not startswith("_")` 过滤后是 60 / 5 —— 差异是私有成员，
        与方法闸门口径一致。**关键断言是「绝大多数成员是被引用的」**：
        如果哪天成员集合全变零引用，说明扫描器的属性证据链断了。
        """
        allm = list(_class_upper_members())
        zero = list(_zero_ref_class_upper_members())
        assert len(allm) > 20, "成员集合太小，扫描器可能没扫到（%d）" % len(allm)
        assert len(zero) < len(allm), (
            "所有成员都被判零引用——属性证据链断了（教训 #30：零命中不可信）")
        assert len(allm) - len(zero) > len(allm) * 0.5, (
            "被引用成员不到一半（%d/%d）——度量可疑，先查 _references"
            % (len(allm) - len(zero), len(allm)))


class TestClassMethodGate:
    """G. 类方法零调用闸门（2026-10-13 P3，棘轮式）。

    补的是零调用闸门的**覆盖率上限**：`_agent_public_functions()` 只扫模块级函数，
    类方法（628 个公共方法）完全不在覆盖内。扩进来后实测 **79 个零引用**，
    按主人拍板以基线**非破坏性冻结**——闸门只拦新增。

    三条测试分工：
      ① test_no_new_dead_class_methods —— 闸门本体（基线外新增 → 红）；
      ② test_baseline_entries_are_still_valid —— 防基线腐烂（真死变已删/已接线 → 红）；
      ③ test_gate_sees_a_planted_dead_method —— **阳性对照**。
    ③ 是上游验收标准「故意种一个死方法 → 闸门必须红」的机器化。没有它，①②
    可能从上线起就恒绿（教训 #30：只测「跑起来没报错」等于没测）。
    """

    def test_no_new_dead_class_methods(self):
        found = {(f, n) for f, _, _, n in _zero_ref_class_methods()}
        new = sorted(found - set(CLASS_METHOD_BASELINE))
        assert not new, (
            "接线审计（教训#14）：以下 agent/ 类公共方法零引用，且不在基线上——\n"
            + "\n".join("  %s :: %s" % (f, n) for f, n in new)
            + "\n处理：接线（谁该调用它）或归档（删除，反模式#6）；"
            "确认为待接线能力则登记进 CLASS_METHOD_BASELINE 并写明理由。"
            "\n**不要**为了变绿去扩大基线——基线只许变小。")

    def test_baseline_entries_are_still_valid(self):
        """基线只许变小：方法被删、或已被接线，都必须把条目删掉。"""
        existing = {(f, n) for f, _, _, n in _class_public_methods()}
        found = {(f, n) for f, _, _, n in _zero_ref_class_methods()}
        gone = sorted(k for k in CLASS_METHOD_BASELINE if k not in existing)
        assert not gone, (
            "基线登记了**已不存在**的方法——方法已归档，删除对应基线条目：\n"
            + "\n".join("  %s :: %s" % k for k in gone))
        wired = sorted(k for k in CLASS_METHOD_BASELINE
                       if k in existing and k not in found)
        assert not wired, (
            "基线登记了**已被接线**的方法——棘轮要收紧，删除对应基线条目：\n"
            + "\n".join("  %s :: %s" % k for k in wired)
            + "\n（条目留着不删，基线就会慢慢变成一句假话——教训 #35）")

    def test_gate_sees_a_planted_dead_method(self):
        """阳性对照（上游验收标准）：种一个死方法，闸门必须看见。

        同时带两条阴性对照：被调用的方法不许误报、私有方法不进公共闸门。
        """
        srcs = {
            "agent/__planted__.py": ast.parse(
                "class C:\n"
                "    def alive(self):\n"
                "        return 1\n"
                "    def dead_one(self):\n"
                "        return 2\n"
                "    def _private_dead(self):\n"
                "        return 3\n"
                "\n"
                "def use(c):\n"
                "    return c.alive()\n"),
        }
        found = {(f, n) for f, _, _, n in _zero_ref_class_methods(srcs)}
        assert ("agent/__planted__.py", "dead_one") in found, (
            "种进去的死方法没被看见——这条闸门是假的（教训 #30）")
        assert ("agent/__planted__.py", "alive") not in found, (
            "被 .alive() 调用的方法被误判成死代码")
        assert ("agent/__planted__.py", "_private_dead") not in found, (
            "私有方法不该进公共方法闸门")

    def test_bare_name_call_is_not_method_evidence(self):
        """阴性对照（2026-10-14 台账 T2.3）：外来裸名不构成「方法被调用」的证据。

        现场：新增 tools/长期优化快照.py 里的模块级 def snapshot()（本文件从不 import 它）
        把基线条目 ('agent/knowledge_index.py','snapshot') 判成「已接线」，闸门假红——
        根因是 _references 按**裸标识符名**收集，而裸名 snapshot() 只可能指向模块级函数，
        不可能指向 SomeClass.snapshot。

        本测试种一个**只含裸名调用**的文件，断言目标方法**仍**被判零引用；
        同时断言**属性调用**（真形态）**能**把它捞出来——否则修复就变成「闸门永远看不见」。
        """
        cls_src = chr(10).join([
            "class C:",
            "    def snapshot(self):",
            "        return 1",
        ])
        bare = {
            "agent/__planted__.py": ast.parse(cls_src),
            "tools/__planted_bare__.py": ast.parse("snapshot()"),
        }
        found = {(f, n) for f, _, _, n in _zero_ref_class_methods(bare)}
        assert ("agent/__planted__.py", "snapshot") in found, (
            "裸名 snapshot() 被当成了方法证据——闸门会对同名模块级函数假红（T2.3）")

        attr_src = chr(10).join([
            "c = object()",
            "c.snapshot()",
        ])
        wired = {
            "agent/__planted__.py": ast.parse(cls_src),
            "tools/__planted_attr__.py": ast.parse(attr_src),
        }
        found2 = {(f, n) for f, _, _, n in _zero_ref_class_methods(wired)}
        assert ("agent/__planted__.py", "snapshot") not in found2, (
            "属性调用 c.snapshot() 没被认出来——闸门被改瞎了")


class TestMoodStyleTrapStaysDeleted:
    """T63（2026-10-23）：PersonalityEngine.get_mood_style 已删除，不许复活。

    它是一段**零引用的陷阱**：从 7 条写死的心情文案里 random.choice 抽一条
    （"今天心情超好，说话甜度+50%！" 之类）。当前它没有任何调用方，所以不是缺陷；
    但谁把它接进 system prompt，就等于**让系统随机捏造她的心情**——而心情的真值
    在 agent/mood.py 的三维模型里（energy/valence/arousal），不是一张文案表。

    这正是 CLAUDE.md 教训 #9/#10 的形态（系统替 LLM 做决定）+ 本仓反复踩的
    「写了但接不上」的另一面：**接不上时它是死的，一旦被接上就是错的**。
    所以处置是**删除**而不是登记待接线——保留一个「看起来能用」的随机心情源，
    下一个人接手时几乎必然把它接进去（T2.1 记录里「待接线能力」的清单已经够长）。

    ⚠ **不要「恢复」它**。真要给她心情，读 mood.py 的真实状态。
    """

    def test_get_mood_style_is_gone(self):
        from agent.personality import PersonalityEngine
        assert not hasattr(PersonalityEngine, "get_mood_style"), (
            "get_mood_style 复活了。它是「系统随机捏造心情」的陷阱——"
            "心情真值在 agent/mood.py 的三维模型里，不要用写死的文案表。")

    def test_personality_does_not_import_random(self):
        """random 随该方法一起消失；它若回来，说明有人又在人格层掷骰子。"""
        src = _parsed["agent/personality.py"]
        imported = set()
        for node in ast.walk(src):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "random" not in imported, (
            "agent/personality.py 又 import 了 random——人格层不许掷骰子"
            "（教训 #9/#10：系统不替 LLM 做决定）。")

class TestLegacyLayeredBudgetStaysDeleted:
    """T2.3（2026-10-23）：旧分层预算的残留已删，不许复活。

    `_assemble_with_budget` 零调用（全仓 0 个调用方），`BUDGET_CONVERSATION` 零读者
    （同组 CORE/RELEVANT/SUPPLEMENTARY/TOTAL 都有生产用途）。两者是**同一套没落地的
    旧预算布局**——删掉它们不是风格问题：留着的话，下一个要实现「对话预算」的人
    会把它接进来，那时项目就有**第三套**预算逻辑（build() 的 budget_limit、
    protocols.fit_backgrounds 的 1200t 池、再加上它），而本仓的历史教训正是
    「同一件事两套逻辑各自漂移」（T10/T12/T25/T35/T42）。

    全项目评估结论同此：docs/开发规划/全项目审查历史_合并.md:130「应删除而非接入」。
    """

    def test_assemble_with_budget_is_gone(self):
        from agent.context_builder import ContextBuilder
        assert not hasattr(ContextBuilder, "_assemble_with_budget"), (
            "_assemble_with_budget 复活了。它零调用，且与 build() 的预算逻辑重复——"
            "要加预算请改 build() 或 fit_backgrounds，不要另立第三套。")

    def test_budget_conversation_is_gone(self):
        from agent.context_builder import ContextBuilder
        assert not hasattr(ContextBuilder, "BUDGET_CONVERSATION"), (
            "BUDGET_CONVERSATION 复活了。它没有任何读者——"
            "对话预算走 build() 的 history 段与 fit_backgrounds 的池子。")

    def test_no_third_budget_authority_in_context_builder(self):
        """结构闸门：ContextBuilder 里「预算」只许有一套入口。

        判据取「类里有多少个方法名/常量名同时含 budget 与 assemble」——
        出现第二个「分段组装预算」实现即红。
        """
        src = _parsed["agent/context_builder.py"]
        offenders = []
        for node in ast.walk(src):
            if isinstance(node, ast.ClassDef) and node.name == "ContextBuilder":
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if "assemble" in child.name.lower() and "budget" in child.name.lower():
                            offenders.append(child.name)
        assert not offenders, (
            "ContextBuilder 又出现了「分段组装 + 预算」方法：" + ", ".join(offenders) +
            "\n本仓已有两套预算（build() 的 budget_limit / fit_backgrounds 的 1200t 池），"
            "第三套必然漂移（T10/T12/T25/T35/T42 同族）。")

class TestPrivateMethodGate:
    """H. 私有方法零调用闸门（2026-10-23 T2.2，棘轮式）。

    T2 归档的 7 条死代码**全是私有**，而当时闸门的扫描条件带
    `not name.startswith('_')` ⇒ 私有方法全在覆盖外，闸门从头到尾没见过它们。
    这是审计说的「闸门有覆盖率上限」的最后一个缺口（T3.3 已补枚举成员）。

    三条测试分工：
      ① test_no_new_dead_private_methods —— 闸门本体（基线外新增 → 红）；
      ② test_private_baseline_entries_are_still_valid —— 防基线腐烂；
      ③ test_gate_sees_a_planted_dead_private_method —— **阳性对照**。
    ③ 是「闸门必须能红」的机器化（教训 #30/#37）——没有它，①② 可能从上线起恒绿。
    """

    def test_no_new_dead_private_methods(self):
        found = {(f, n) for f, _, _, n in _zero_ref_class_methods(include_private=True)
                 if _is_private_method_name(n)}
        new = sorted(found - set(PRIVATE_METHOD_BASELINE))
        assert not new, (
            "以下 agent/ 私有方法零引用，且不在基线内——\n"
            + "\n".join("  %s :: %s" % (f, n) for f, n in new)
            + "\n处理：接线，或删除（同步版被 _async 取代是最常见的一类）；"
            "确认为待接线能力才登记进 PRIVATE_METHOD_BASELINE 并写明理由。"
            "\n**不要**为了变绿去扩大基线——基线只许变小。")

    def test_private_baseline_entries_are_still_valid(self):
        """基线只许变小：方法被删、或已被接线，都必须把条目删掉。"""
        existing = {(f, n) for f, _, _, n in _class_public_methods(include_private=True)}
        found = {(f, n) for f, _, _, n in _zero_ref_class_methods(include_private=True)}
        gone = sorted(k for k in PRIVATE_METHOD_BASELINE if k not in existing)
        assert not gone, ("基线里有已被删除的私有方法，请在 PRIVATE_METHOD_BASELINE 里删掉：\n"
                          + "\n".join("  %s :: %s" % (f, n) for f, n in gone))
        wired = sorted(k for k in PRIVATE_METHOD_BASELINE if k in existing and k not in found)
        assert not wired, ("这些私有方法已经接线了，请从基线里删掉（棘轮只许变小）：\n"
                           + "\n".join("  %s :: %s" % (f, n) for f, n in wired))

    def test_gate_sees_a_planted_dead_private_method(self):
        """**阳性对照**：种一个真死私有方法进去，闸门必须抓到它。"""
        src = chr(10).join([
            "class C:",
            "    def _live(self):",
            "        return 1",
            "    def _dead(self):",
            "        return 2",
            "    ",
            "c = object()",
            "c._live()",
        ])
        planted = {"agent/__planted_private__.py": ast.parse(src)}
        found = {(f, n) for f, _, _, n in _zero_ref_class_methods(
            planted, include_private=True)}
        assert ("agent/__planted_private__.py", "_dead") in found, (
            "私有零引用方法没被闸门抓到——扩覆盖是假的")
        assert ("agent/__planted_private__.py", "_live") not in found, (
            "被属性调用的私有方法被误判成零引用——闸门会制造假红")

    def test_dead_class_entries_have_no_test_refs(self):
        """**D 类条目必须真的没有任何测试引用**（2026-10-23 T2.2 踩坑后的闸门）。

        为什么需要这一条：第一版基线把 14 条全标成"死代码"，而其中 10 条在 tests/
        里**有真实调用**——照"零引用"直接删，当场打红 8 条测试。本闸门的 _parsed
        不含 tests/，所以它报的零引用只覆盖生产代码。

        两个方向都钉：
          · 标 D（真死）的，tests/ 里必须确实没有引用 —— 否则它是「仅测试引用」，
            标错会诱导后人删掉被测试守护的方法；
          · 标 T（仅测试引用）的，tests/ 里必须确实有引用 —— 否则它其实可以直接删，
            标错会让死代码永久冻结（棘轮只许变小，标错了就永远清不掉）。
        基线理由若因测试改名/删除而腐烂，这条会红。
        """
        wrong_d = []
        wrong_t = []
        for (f, name), reason in PRIVATE_METHOD_BASELINE.items():
            has_test_ref = bool(_TEST_REFS.get(name))
            is_dead_class = reason.startswith("D 类")
            if is_dead_class and has_test_ref:
                wrong_d.append("%s :: %s %s" % (f, name, sorted(_TEST_REFS[name])))
            if (not is_dead_class) and (not has_test_ref):
                wrong_t.append("%s :: %s" % (f, name))
        assert not wrong_d, (
            "这些条目标了「D 类（真死）」但 tests/ 里有调用——照它删会打红测试：\n  "
            + "\n  ".join(wrong_d)
            + "\n改标 T 类（仅测试引用）并写明是哪个测试在调。")
        assert not wrong_t, (
            "这些条目标了「T 类（仅测试引用）」但 tests/ 里已经没人调了——"
            "它们现在可以直接删：\n  " + "\n  ".join(wrong_t))

    def test_dunder_methods_are_not_gated(self):
        """dunder 必须排除：__init__ 之类由解释器/框架调用，静态引用看不到。"""
        src = ast.parse(chr(10).join([
            "class C:",
            "    def __init__(self):",
            "        self.x = 1",
        ]))
        planted = {"agent/__planted_dunder__.py": src}
        found = {(f, n) for f, _, _, n in _zero_ref_class_methods(
            planted, include_private=True)}
        assert ("agent/__planted_dunder__.py", "__init__") not in found, (
            "dunder 被纳入闸门了——会产出几十条无法消除的噪声")

