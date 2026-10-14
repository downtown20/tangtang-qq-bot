"""知识/附件信任边界回归测试。"""

import asyncio
import inspect
from types import SimpleNamespace

from agent.handler import MessageHandler
from agent.knowledge import KnowledgeBase


def test_uploaded_file_is_turn_local_and_never_promoted_to_global_kb(tmp_path, monkeypatch):
    """普通群友附件只供当前回合阅读，不能自动写入全局 knowledge。"""
    monkeypatch.chdir(tmp_path)
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    uploaded = tmp_path / "活动方案.txt"
    uploaded.write_text("周六下午三点集合", encoding="utf-8")

    class NapCat:
        async def download_file(self, _file_id, **_kwargs):
            return str(uploaded)

    knowledge = SimpleNamespace(reload_calls=0)

    def reload_knowledge():
        knowledge.reload_calls += 1

    async def analyze_document(*_args, **_kwargs):
        return None

    knowledge.reload = reload_knowledge
    handler = SimpleNamespace(
        napcat=NapCat(),
        knowledge=knowledge,
        _last_doc_by_context={},
        _safe_task=lambda coro, **_kwargs: coro.close(),
        _analyze_and_index_document=analyze_document,
        _warm_knowledge_embeddings=lambda: None,
    )

    result = asyncio.run(MessageHandler._handle_file_message(
        handler,
        text="[文件:活动方案.txt|file_id=file-1]",
        user_id="user-A",
        group_id="group-1",
        context_key="group-1",
    ))

    assert list(knowledge_dir.iterdir()) == []
    assert knowledge.reload_calls == 0
    assert "周六下午三点集合" in result
    assert "自动存入知识库" not in result
    assert "不可信数据" in result


def test_private_file_processing_happens_after_robot_and_blacklist_guards():
    """黑名单/机器人私聊必须在下载文件前 fail-closed。"""
    source = inspect.getsource(MessageHandler.handle_private_message)
    file_pos = source.index("await self._handle_file_message")

    assert source.index("if user_id in self._robot_ids") < file_pos
    assert source.index("if user_id in self._private_blacklist") < file_pos


def test_general_knowledge_index_excludes_sensitive_directories(tmp_path):
    """普通 search_knowledge 不得加载成人专用资料。"""
    (tmp_path / "公共指南.md").write_text(
        "## 公共功能\n糖糖可以查天气", encoding="utf-8",
    )
    sensitive = tmp_path / "色色参考"
    # 2026-10-14 T47：契约改为「文件夹 = 注入槽位」——
    # 文件必须放进数字前缀的槽位目录才会被注入（根下的平铺文件不注入）。
    slot = sensitive / "00-核心"
    slot.mkdir(parents=True)
    (slot / "亲密写作.md").write_text(
        "## 成人内容\n这里只能在已授权私聊场景使用", encoding="utf-8",
    )

    kb = KnowledgeBase(str(tmp_path))

    assert "查天气" in kb.search("公共指南")
    assert kb.search("亲密写作") == ""
    assert all("成人内容" not in chunk.content for chunk in kb.chunks)


def test_explicit_private_scenario_can_load_sensitive_reference(tmp_path):
    """从公共索引隔离不等于删除：已授权私聊场景仍可走专用加载器。"""
    sensitive = tmp_path / "色色参考"
    # 2026-10-14 T47：契约改为「文件夹 = 注入槽位」——
    # 文件必须放进数字前缀的槽位目录才会被注入（根下的平铺文件不注入）。
    slot = sensitive / "00-核心"
    slot.mkdir(parents=True)
    (slot / "亲密写作.md").write_text(
        "## 私密参考\n仅供明确授权的私聊场景", encoding="utf-8",
    )
    handler = SimpleNamespace(knowledge=KnowledgeBase(str(tmp_path)))

    result = MessageHandler._seductive_knowledge(handler)

    assert "仅供明确授权的私聊场景" in result


def test_sensitive_reference_loading_yields_to_event_loop(tmp_path, monkeypatch):
    """敏感参考加载不能把同步文件读取放进私聊消息协程。"""
    sensitive = tmp_path / "色色参考"
    sensitive.mkdir()
    for index in range(8):
        (sensitive / f"参考{index}.md").write_text(
            "仅供授权场景使用", encoding="utf-8",
        )

    original_read_text = type((sensitive / "参考0.md")).read_text

    def slow_read_text(path, *args, **kwargs):
        import time
        time.sleep(0.02)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type((sensitive / "参考0.md")), "read_text", slow_read_text)
    handler = SimpleNamespace(knowledge=SimpleNamespace(knowledge_dir=str(tmp_path)))
    handler._seductive_knowledge = lambda: MessageHandler._seductive_knowledge(handler)

    async def exercise():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.005)
                ticks += 1

        async def load():
            return await MessageHandler._seductive_knowledge_async(handler)

        heartbeat_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        load_task = asyncio.create_task(load())
        await asyncio.sleep(0.08)
        ticks_during_load = ticks
        await asyncio.gather(load_task, heartbeat_task)
        return ticks_during_load

    ticks_during_load = asyncio.run(exercise())

    assert ticks_during_load >= 5


def test_document_summary_analysis_never_overwrites_source(tmp_path):
    """LLM 摘要只能观测，不能把未经核验内容写回知识原文或触发 reload。"""
    source = tmp_path / "活动方案.md"
    original = "## 活动\n周六下午三点集合\n"
    source.write_text(original, encoding="utf-8")
    reload_calls = []

    async def call_llm_light(**_kwargs):
        return "- 时间：周六下午三点\n- 地点：未知（不得补写）"

    handler = SimpleNamespace(
        _call_llm_light=call_llm_light,
        knowledge=SimpleNamespace(reload=lambda: reload_calls.append(True)),
    )

    asyncio.run(MessageHandler._analyze_and_index_document(
        handler, str(source), original, len(original),
    ))

    assert source.read_text(encoding="utf-8") == original
    assert reload_calls == []




def test_flat_file_under_root_is_not_injected(tmp_path):
    """新契约的反向：根下的平铺文件不注入（只有数字前缀槽位才注入）。

    为什么钉这条：旧实现是 rglob 全扫。改造后若不慎回退成「全扫」，
    归档目录 _原文/ 会被一起注入 —— 那是蒸馏前的原文，不该进上下文，
    而且 `_原文` 里有本仓刻意排除的内容（如 CG 系统说明）。
    """
    sensitive = tmp_path / "色色参考"
    sensitive.mkdir()
    (sensitive / "平铺在根下.md").write_text("ROOT_LEVEL_CONTENT", encoding="utf-8")
    arch = sensitive / "_原文"
    arch.mkdir()
    (arch / "归档.md").write_text("ARCHIVE_CONTENT", encoding="utf-8")
    handler = SimpleNamespace(knowledge=KnowledgeBase(str(tmp_path)))
    result = MessageHandler._seductive_knowledge(handler)
    assert "ROOT_LEVEL_CONTENT" not in result, "根下平铺文件被注入了 —— 槽位契约被绕过"
    assert "ARCHIVE_CONTENT" not in result, "_原文 归档被注入了 —— 会泄漏蒸馏前原文"
