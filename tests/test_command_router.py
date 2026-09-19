"""命令路由回归测试——钉住模块级 import 缺失类事故。

2026-08-16 事故：handler 拆分后 handler_commands.py 缺失 import re /
Path / Relationship——/说 命令一调用就 NameError: name 're' is not defined
（pyflakes 未装时 273 测试全绿没拦住，因为调用期才炸）。
本文件走三个 /命令 的参数解析分支，缺 import 立刻红。
"""
import asyncio
import ast
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
