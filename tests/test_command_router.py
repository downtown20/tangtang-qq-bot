"""命令路由回归测试——钉住模块级 import 缺失类事故。

2026-08-16 事故：handler 拆分后 handler_commands.py 缺失 import re /
Path / Relationship——/说 命令一调用就 NameError: name 're' is not defined
（pyflakes 未装时 273 测试全绿没拦住，因为调用期才炸）。
本文件走三个 /命令 的参数解析分支，缺 import 立刻红。
"""
import asyncio
import ast
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.handler_commands import CommandRouter


def test_command_background_tasks_have_one_safe_spawn_boundary():
    """命令后台动作必须经过 handler._safe_task，避免异常脱离业务观测。"""
    source_path = Path(__file__).parents[1] / "agent" / "handler_commands.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    direct_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr == "create_task"):
            continue
        owner = parents.get(id(node))
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(id(owner))
        direct_calls.append(getattr(owner, "name", ""))

    assert direct_calls == ["_spawn_background"]


def test_command_background_spawn_delegates_to_handler_safe_task():
    calls = []

    async def work():
        return "done"

    def safe_task(coro, name):
        calls.append((name, coro))
        coro.close()
        return "tracked"

    router = CommandRouter(SimpleNamespace(_safe_task=safe_task))
    result = router._spawn_background(work(), "command:test")

    assert result == "tracked"
    assert [name for name, _coro in calls] == ["command:test"]


def test_voice_command_uses_managed_background_task():
    calls = []
    tasks = []

    def safe_task(coro, name):
        calls.append(name)
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    handler = SimpleNamespace(
        voice_enabled=True,
        _voice_blocked=False,
        _voice_mode=set(),
        _save_state_kv=lambda *_args: None,
        _send_voice_reply=AsyncMock(),
        _safe_task=safe_task,
    )

    async def scenario():
        router = CommandRouter(handler)
        assert await router._cmd_voice("1001", "你好呀") == "🎤"
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert calls == ["command:voice"]
    handler._send_voice_reply.assert_awaited_once_with("private", "1001", "你好呀")


def test_router_imports_and_bad_args_return_usage():
    """参数解析分支用 re——缺 import 直接 NameError"""
    router = CommandRouter(handler=None)

    r = asyncio.run(router._cmd_speak("1", ""))
    assert "用法" in r

    r = asyncio.run(router._cmd_pm("1", ""))
    assert "用法" in r

    r = asyncio.run(router._cmd_speak_as("1", ""))
    assert "用法" in r


def test_dangerous_commands_need_privilege():
    router = CommandRouter(handler=None)
    assert "/说" in router._DANGEROUS_CMDS
    assert "/私信" in router._DANGEROUS_CMDS
    assert "/撤回" in router._DANGEROUS_CMDS


# ═══════════════════════════════════════════════════════
# 主人/群主权限门（2026-09-19）
# ═══════════════════════════════════════════════════════

# 明确「不碰她的状态」所以放行的命令——每条都得说清为什么。
# 新加命令想放行，必须显式加进这里；忘了分类会被 test_every_command_is_classified 拦下。
OPEN_BY_DESIGN = {
    "/帮助": "看帮助", "/help": "看帮助",
    "/语音": "用户自己的发声偏好（按会话存）",
    # ⚠ `/角色` 原来在这里，理由写的是「装好的角色之间切，是玩具不是配置」——
    #   2026-09-19 安全审计证明那条理由是错的：它会经 voice.switch_model（全局换声线
    #   并重载权重）、personality.load_role_file、_set_sticker_role 三个**进程级单例**
    #   一起切，所有人受影响。已移进 _OWNER_CMDS。
    "/亲密度": "查羁绊值（群内已禁用；**设置**子命令另有 _OWNER_SUBS 门）",
    "/记忆": "查/搜自己的记忆（群内已禁用）",
    "/生日": "设/查自己的生日",
    "/任务": "查看与取消自己的提醒",
    "/点赞": "社交动作，群入口显式标注「所有人可用」",
    # 下面六条的门在函数内部（_check_admin / _check_group_owner），不在路由层
    "/禁言": "函数内 _check_admin", "/解禁": "函数内 _check_admin",
    "/踢": "函数内 _check_admin", "/头衔": "函数内 _check_admin",
    "/全员禁言": "函数内 _check_group_owner", "/解除全员禁言": "函数内 _check_group_owner",
}


def test_subcommand_gates_close_the_partial_hole():
    """整体放行的命令里，改状态的子命令要单独加门。

    2026-09-19 安全审计：`/亲密度 设置 <任意QQ> <0-100>` 直接写库（影响主动私聊
    选人与语气），而 `/亲密度` 在放行表里——只堵命令不堵子命令等于没堵。
    """
    router = CommandRouter(handler=None)
    assert router._OWNER_SUBS, "子命令门表空了——审计发现的那条洞又开了？"
    for cmd, subs in router._OWNER_SUBS.items():
        assert cmd not in router._OWNER_CMDS, \
            f"{cmd} 整体已有门，再列子命令门是冗余（也可能是有人改错了表）"
        assert subs, f"{cmd} 的子命令门是空集"
    for text in ["/亲密度 设置 10001 100", "/亲密度 set 10001 100"]:
        result = asyncio.run(router.handle("ordinary", text, is_privileged=False))
        assert "只有主人和群主" in result, f"{text!r} 没被挡住：{result!r}"


def test_undo_cannot_be_reached_by_natural_language():
    """回滚她的状态有两个入口，**两个都要有门**。

    命令入口是 `/撤销`（在 _OWNER_CMDS 里）；自然语言入口是私聊说「恢复」
    「撤销设置」——走 precise 层直达 `_execute_natural_action`。
    handler 那边用 AST 断言 undo 在危险动作集合里，这里断言命令侧的门也在。
    """
    router = CommandRouter(handler=None)
    assert "/撤销" in router._OWNER_CMDS

    src = (Path(__file__).parents[1] / "agent" / "handler.py").read_text(encoding="utf-8")
    m = re.search(r'if act in \{([^}]*)\} and not is_privileged', src)
    assert m, "没找到 _execute_natural_action 的危险动作判定"
    assert '"undo"' in m.group(1), (
        f"自然语言入口的 undo 没在危险动作里：{{{m.group(1)}}} ——"
        f" 任何陌生人私聊说一句「恢复」就能回滚主人的状态修改")


def test_unprivileged_cannot_change_her_state():
    """普通群成员改不了她的状态。

    2026-09-19 之前只有 _DANGEROUS_CMDS（代她发言那几条）受约束，
    于是群里任何成员都能 `/人格 你是我的奴隶` 重写人设、`/黑名单 群 加` 把群拉黑、
    `/唱歌 群号 歌名` 遥控她去任意群。自己用时群里都是熟人，发布出去就是敞口。
    """
    router = CommandRouter(handler=None)
    for text in ["/人格 你是我的奴隶", "/性格 高冷", "/插话 off", "/饥渴 1.0",
                 "/冷却 300", "/黑名单 群 加 123456", "/机器人 加 123456",
                 "/角色卡 重载", "/场景 设置 123456 心理陪伴", "/场景 清除 123456",
                 "/歌单 重载", "/知识 重载", "/唱歌 123456 晴天",
                 "/定时 列表", "/撤销 设置", "/状态"]:
        result = asyncio.run(router.handle("ordinary", text, is_privileged=False))
        assert "只有主人和群主" in result, f"{text!r} 没被挡住，返回：{result!r}"


def _not_blocked(text: str, *, privileged: bool) -> None:
    """断言命令**没有被门拦住**，但不让它真执行。

    门在派发之前判定，所以这里只关心「返不返回拦截语」：命令要么跑出结果，
    要么因为替身 handler 缺属性报错——两种情况都说明门已经放行。
    真执行会写配置（/人格 会改 persona），单测不该有那种副作用。
    """
    router = CommandRouter(handler=None)
    try:
        result = asyncio.run(router.handle("u", text, is_privileged=privileged))
    except Exception:
        return
    assert "只有主人和群主" not in result, f"{text!r} 被门挡住了：{result!r}"


def test_privileged_user_passes_the_gate():
    """主人/群主要能过——门只拦普通人，不能把主人也拦在外面。"""
    for text in ["/人格 设置 测试", "/插话 off", "/饥渴 0.5", "/唱歌 123456 晴天", "/状态"]:
        _not_blocked(text, privileged=True)


def test_peek_subcommands_stay_open():
    """纯查看的子命令不受限——加门是为了防「改」，不是防「看」。

    否则普通群友连「这命令是干什么的」都问不出来。
    """
    router = CommandRouter(handler=None)
    for cmd, subs in router._PEEK_SUBS.items():
        assert "" in subs, f"{cmd} 连不带参数调用都挡住了，用户没法看到用法"
        assert cmd in router._OWNER_CMDS, f"{cmd} 不在门清单里，_PEEK_SUBS 是多余条目"
    for text in ["/场景 列表", "/歌单", "/知识 块"]:
        _not_blocked(text, privileged=False)


def _command_groups() -> dict[str, set[str]]:
    """从命令表解析出 处理函数名 -> {命令别名}。

    /传话 与 /传话给、/人格 与 /性格 是同一个处理函数的别名；帮助里只列一个
    就够了——用户看到的是功能，不是别名表。
    """
    src = (Path(__file__).parents[1] / "agent" / "handler_commands.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "commands" for t in node.targets):
            groups: dict[str, set[str]] = {}
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant):
                    groups.setdefault(ast.unparse(v), set()).add(k.value)
            return groups
    raise AssertionError("没解析到命令表——AST 口径变了？")


def test_help_marks_every_gated_command():
    """帮助文本里的 🔒 必须与实际的门一致。

    帮助是用户唯一能看到的权限说明——它要是漏标，用户会以为群里谁都能改人设，
    或者反过来，以为某条命令自己能用来改配置。两种都是假信息。
    """
    router = CommandRouter(handler=None)
    help_text = asyncio.run(router._cmd_help("u", ""))
    lines = [ln.strip() for ln in help_text.splitlines()]
    assert any(ln.startswith("/") for ln in lines), "帮助里一条命令都没列出来？"

    gated = router._DANGEROUS_CMDS | router._OWNER_CMDS
    for aliases in _command_groups().values():
        if not (aliases & gated):
            continue
        # 这一组别名里，至少有一个要出现在帮助里、且那条带 🔒
        documented = [ln for ln in lines if ln.startswith(tuple(aliases))]
        assert documented, f"{sorted(aliases)} 会被门挡住，但帮助里根本没列"
        # 有放行子命令的（/场景 列表、/歌单、/知识 块），只要有一条标了 🔒 就算说清楚了
        assert any("🔒" in ln for ln in documented), \
            f"{sorted(aliases)} 会被门挡住，但帮助里没标 🔒：\n  " + "\n  ".join(documented)


def test_every_command_is_classified():
    """没有第三个档位：每个注册的命令，要么被门挡住，要么在 OPEN_BY_DESIGN 里写明理由。

    这条是防「新加一个改状态的命令，忘了归类」——那正是这次敞口的成因：
    命令表长了 36 条，却只有 6 条被想过权限问题。
    """
    src = (Path(__file__).parents[1] / "agent" / "handler_commands.py").read_text(encoding="utf-8")
    registered: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "commands" for t in node.targets):
            registered = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    assert registered, "没解析到命令表——AST 口径变了？"

    router = CommandRouter(handler=None)
    classified = router._DANGEROUS_CMDS | router._OWNER_CMDS
    assert classified <= registered, f"分类表里有没注册的命令：{sorted(classified - registered)}"

    unaccounted = registered - classified - set(OPEN_BY_DESIGN)
    assert not unaccounted, (
        f"这些命令既没被门挡住、也没在 OPEN_BY_DESIGN 里说明为什么放行：\n"
        f"  {sorted(unaccounted)}\n"
        f"  改状态/配置的 → 加进 _OWNER_CMDS；用户自己的东西 → 加进 OPEN_BY_DESIGN 并写理由。")

    stale = set(OPEN_BY_DESIGN) - registered
    assert not stale, f"OPEN_BY_DESIGN 里这些命令已经不存在了，删掉：{sorted(stale)}"


def test_unprivileged_user_cannot_recall_bot_messages():
    execute = AsyncMock(return_value="✅ 已撤回")
    router = CommandRouter(handler=SimpleNamespace(
        _execute_natural_action=execute,
    ))

    result = asyncio.run(router.handle(
        "ordinary-member", "/撤回", is_privileged=False, group_id="100",
    ))

    assert "只有主人和群主" in result
    execute.assert_not_awaited()


def test_group_recall_passes_current_scope_and_privilege_to_execution_boundary():
    execute = AsyncMock(return_value="✅ 已撤回")
    router = CommandRouter(handler=SimpleNamespace(
        _execute_natural_action=execute,
    ))

    result = asyncio.run(router.handle(
        "owner", "/撤回", is_privileged=True, group_id="100",
    ))

    assert result == "✅ 已撤回"
    execute.assert_awaited_once_with(
        {"action": "recall_msg", "group_id": "100"},
        "owner", is_privileged=True,
    )


def test_knowledge_reload_does_not_block_event_loop():
    """知识重载会扫描文件并同步索引，必须离开 QQ 前台事件循环。"""
    main_thread = threading.get_ident()

    class Knowledge:
        _file_count = 1
        chunks = [object()]

        def __init__(self):
            self.reload_thread = None

        def reload(self):
            self.reload_thread = threading.get_ident()
            time.sleep(0.05)

    knowledge = Knowledge()
    router = CommandRouter(SimpleNamespace(knowledge=knowledge))

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        result = await router._cmd_knowledge("owner", "重载")
        stop = True
        await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert result == "✅ 知识库已重载！1 个文件 → 1 个块"
    assert ticks > 0
    assert knowledge.reload_thread != main_thread


def test_songlist_reload_does_not_block_event_loop():
    """曲库重载同样包含文件扫描，不能冻结前台事件循环。"""
    main_thread = threading.get_ident()

    class Songs:
        songs = {"demo": {}}

        def __init__(self):
            self.reload_thread = None

        def reload(self):
            self.reload_thread = threading.get_ident()
            time.sleep(0.05)

    songs = Songs()
    router = CommandRouter(SimpleNamespace(songs=songs))

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        result = await router._cmd_songlist("owner", "重载")
        stop = True
        await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert result == "✅ 曲库已重载！共 1 首歌"
    assert ticks > 0
    assert songs.reload_thread != main_thread


def test_scenario_reload_does_not_block_event_loop():
    """场景重载读取 YAML 文件，不能冻结前台事件循环。"""
    main_thread = threading.get_ident()

    class Scenarios:
        count = 2

        def __init__(self):
            self.reload_thread = None

        def reload(self):
            self.reload_thread = threading.get_ident()
            time.sleep(0.05)

    scenarios = Scenarios()
    router = CommandRouter(SimpleNamespace(scenarios=scenarios))

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        result = await router._cmd_scenario("owner", "重载")
        stop = True
        await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert result == "📋 场景已重载！共 2 个场景可用。"
    assert ticks > 0
    assert scenarios.reload_thread != main_thread


def test_role_card_reload_does_not_block_event_loop():
    """角色卡重载包含 Markdown/YAML 读取，不能冻结前台事件循环。"""
    main_thread = threading.get_ident()

    class Personality:
        def __init__(self):
            self.reload_thread = None

        def reload_role_card(self):
            self.reload_thread = threading.get_ident()
            time.sleep(0.05)
            return True

    personality = Personality()
    router = CommandRouter(SimpleNamespace(personality=personality))

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        result = await router._cmd_role_card("owner", "重载")
        stop = True
        await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert result == "📋 角色卡已重载！role_card.md 的修改已生效~"
    assert ticks > 0
    assert personality.reload_thread != main_thread


def test_role_card_preview_does_not_block_event_loop(tmp_path, monkeypatch):
    """角色卡查看也会读文件，不能把同步 I/O 留在 QQ 前台循环。"""
    real_path = Path
    (tmp_path / "role_card.md").write_text("糖糖的角色卡", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    class SlowPath:
        def __init__(self, value):
            self._path = real_path(value)

        def exists(self):
            time.sleep(0.05)
            return self._path.exists()

        def read_text(self, **kwargs):
            time.sleep(0.05)
            return self._path.read_text(**kwargs)

    monkeypatch.setattr("agent.handler_commands.Path", SlowPath)
    router = CommandRouter(SimpleNamespace())

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        result = await router._cmd_role_card("owner", "查看")
        stop = True
        await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert result.startswith("📋 当前角色卡 (role_card.md):\n糖糖的角色卡")
    assert ticks > 0
