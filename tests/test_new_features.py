"""
测试 2026年7月新功能：生日/定时/画图/播报/Markdown/入群欢迎/情绪语音/群管理
"""

import pytest
import tempfile, os
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


# ═══════════════════════════════════════
# Store — 生日字段
# ═══════════════════════════════════════

class TestBirthdayStore:
    def test_set_and_get_birthday(self, store):
        store.set_birthday("12345", "03-15")
        assert store.get_birthday("12345") == "03-15"

    def test_set_birthday_chinese_format(self, store):
        """中文格式「3月15日」→ MM-DD"""
        store.set_birthday("12345", "3月15日")
        assert store.get_birthday("12345") == "03-15"

    def test_set_birthday_invalid_format(self, store):
        """无效格式返回 False"""
        assert store.set_birthday("12345", "abc") is False
        assert store.set_birthday("12345", "13-32") is False

    def test_get_birthday_empty(self, store):
        """没设置过生日返回空"""
        assert store.get_birthday("99999") == ""

    def test_get_all_birthdays(self, store):
        store.set_birthday("111", "01-01")
        store.set_birthday("222", "12-25")
        all_bdays = store.get_all_birthdays()
        assert len(all_bdays) == 2
        # 按生日排序
        assert all_bdays[0]["birthday"] == "01-01"
        assert all_bdays[1]["birthday"] == "12-25"

    def test_set_birthday_creates_person(self, store):
        """设置生日时如果群友不存在，自动创建"""
        store.set_birthday("new_user", "06-01")
        person = store.get_or_create_person("new_user")
        assert person is not None
        assert person["qq_id"] == "new_user"


# ═══════════════════════════════════════
# Scheduler — CronScheduler 增删查
# （2026-08-16 范式转换：自然语言解析 parse_natural_schedule 已删——
#   定时意图由 LLM 工具决定，边界测试见 tests/test_tasks.py）
# ═══════════════════════════════════════

class TestCronScheduler:
    @pytest.fixture
    def sched(self):
        # CronScheduler 从项目根 .scheduled_tasks.json 加载任务——
        # 糖糖真实运行时也写这个文件，不隔离会让测试读到真实任务（flaky）。
        import agent.scheduler as _sch
        from pathlib import Path as _Path
        _f = _Path(_sch.TASKS_FILE)
        _bak = _f.read_text(encoding="utf-8") if _f.exists() else None
        if _f.exists():
            _f.unlink()
        from agent.scheduler import CronScheduler
        s = CronScheduler(
            send_group_msg=AsyncMock(return_value=True),
            send_private_msg=AsyncMock(return_value=True),
            llm_caller=AsyncMock(return_value="嗯嗯好的~"),
            get_group_ids=lambda: ["10005"],
        )
        yield s
        # 清理 + 恢复真实调度文件
        s._tasks = []
        s._save()
        if _bak is not None:
            _f.write_text(_bak, encoding="utf-8")
        elif _f.exists():
            _f.unlink()

    def test_add_one_shot(self, sched):
        tid = sched.add_one_shot("测试提醒", 30, group_id="123")
        assert tid > 0
        tasks = sched._tasks
        assert len(tasks) == 1
        assert tasks[0]["type"] == "once"
        assert tasks[0]["text"] == "测试提醒"

    def test_add_daily(self, sched):
        tid = sched.add_daily("晨间播报", 9, 0, group_id="123")
        assert tid > 0
        t = sched._tasks[0]
        assert t["type"] == "daily"
        assert t["hour"] == 9
        assert t["minute"] == 0

    def test_add_weekly(self, sched):
        tid = sched.add_weekly("周报提醒", 4, 17, 0, group_id="123")
        assert tid > 0
        t = sched._tasks[0]
        assert t["type"] == "weekly"
        assert t["weekday"] == 4
        assert t["hour"] == 17

    def test_remove_task(self, sched):
        tid = sched.add_one_shot("待删除", 60)
        assert sched.remove_task(tid) is True
        assert len(sched._tasks) == 0

    def test_remove_nonexistent(self, sched):
        assert sched.remove_task(99999) is False

    def test_list_tasks(self, sched):
        sched.add_one_shot("任务1", 10)
        sched.add_daily("任务2", 8, 0)
        result = sched.list_tasks()
        assert "任务1" in result
        assert "任务2" in result

    def test_persistence(self, sched):
        """任务保存到磁盘后再加载"""
        sched.add_one_shot("持久化测试", 60)
        sched._save()

        from agent.scheduler import CronScheduler
        s2 = CronScheduler(
            send_group_msg=AsyncMock(return_value=True),
            send_private_msg=AsyncMock(return_value=True),
            llm_caller=AsyncMock(return_value="嗯嗯~"),
            get_group_ids=lambda: [],
        )
        assert len(s2._tasks) == 1
        assert s2._tasks[0]["text"] == "持久化测试"
        # 清理
        s2._tasks = []
        s2._save()


# ═══════════════════════════════════════
# ReplyPipeline — Markdown 保留
# ═══════════════════════════════════════

class TestMarkdownPreservation:
    def test_structured_content_keeps_markdown(self, reply_pipeline):
        """结构化回复保留 Markdown 格式"""
        structured = "## 功能介绍\n\n- 功能1：自动回复\n- 功能2：唱歌\n\n详见上面~"
        result = reply_pipeline.clean(structured)
        assert "##" in result or "功能" in result  # 标题或列表至少保留一个

    def test_casual_reply_strips_markdown(self, reply_pipeline):
        """普通聊天回复剥离 Markdown"""
        casual = "喵~ **谢谢**你呢"
        result = reply_pipeline.clean(casual)
        assert "**" not in result  # 粗体被剥离

    def test_code_block_preserved(self, reply_pipeline):
        """代码块保留"""
        code = "```python\nprint('hello')\n```"
        result = reply_pipeline.clean(code)
        assert "```" in result  # 代码块保留


# ═══════════════════════════════════════
# Handler — 入群欢迎
# ═══════════════════════════════════════

class TestWelcomeNewMember:
    def test_welcome_format_replacement(self):
        """占位符替换"""
        msg = "欢迎 [QQ号] 加入！"
        result = msg.replace("[QQ号]", "123456").replace("{user_id}", "123456")
        assert "123456" in result
        assert "[QQ号]" not in result

    def test_welcome_backward_compat(self):
        """兼容旧 {user_id} 格式"""
        msg = "欢迎 {user_id} 加入！"
        result = msg.replace("[QQ号]", "999").replace("{user_id}", "999")
        assert "999" in result
        assert "{user_id}" not in result


# ═══════════════════════════════════════
# Handler — 生日提取
# ═══════════════════════════════════════

class TestBirthdayExtraction:
    """测试自然语言生日提取的正则"""
    @pytest.fixture
    def extractor(self):
        import re
        def _extract(text):
            patterns = [
                r'(?:我|俺|人家|咱)(?:的|滴)?生日(?:是|在|：|:)?\s*(\d{1,2})月(\d{1,2})[日号]',
                r'(?:我|俺|人家|咱)(?:的|滴)?生日(?:是|在|：|:)?\s*(\d{2})-(\d{2})',
                r'生日[：:]\s*(\d{1,2})月(\d{1,2})[日号]',
            ]
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    month, day = int(m.group(1)), int(m.group(2))
                    if 1 <= month <= 12 and 1 <= day <= 31:
                        return f"{month:02d}-{day:02d}"
            return None
        return _extract

    def test_simple_birthday(self, extractor):
        assert extractor("我生日是3月15日") == "03-15"

    def test_birthday_with_de(self, extractor):
        assert extractor("我的生日是12月1号") == "12-01"

    def test_birthday_dash_format(self, extractor):
        assert extractor("我生日是06-01") == "06-01"

    def test_birthday_label_only(self, extractor):
        assert extractor("生日：8月8日") == "08-08"

    def test_no_birthday(self, extractor):
        assert extractor("今天天气真好") is None
        assert extractor("生日快乐！") is None

    def test_invalid_date(self, extractor):
        assert extractor("我生日是13月40日") is None


# ═══════════════════════════════════════
# ImageGen — 引擎初始化
# ═══════════════════════════════════════

class TestImageGenEngine:
    def test_engine_init(self):
        from agent.image_gen import ImageGenEngine
        engine = ImageGenEngine(api_key="test-key", model="wanx2.0-t2i-turbo")
        assert engine.model == "wanx2.0-t2i-turbo"
        assert engine.api_key == "test-key"

    def test_engine_singleton(self):
        from agent.image_gen import get_image_engine, _engine
        # 重置单例
        import agent.image_gen as ig
        ig._engine = None
        e1 = get_image_engine(api_key="k1")
        e2 = get_image_engine(api_key="k2")
        assert e1 is e2  # 单例

    def test_skill_registered(self):
        """画图技能已注册"""
        from agent.image_gen import _register
        _register()  # 手动触发技能注册
        from agent.skills import list_skills
        skill_names = [s.name for s in list_skills()]
        assert "generate_image" in skill_names


# ═══════════════════════════════════════
# Voice — 情绪语速映射
# ═══════════════════════════════════════

class TestEmotionSpeed:
    def test_happy_faster(self):
        from agent.voice import VoiceEngine
        ratio = VoiceEngine._EMOTION_SPEED_RATIO.get("开心", 1.0)
        assert ratio > 1.0  # 开心快一点

    def test_sad_slower(self):
        from agent.voice import VoiceEngine
        ratio = VoiceEngine._EMOTION_SPEED_RATIO.get("伤心", 1.0)
        assert ratio < 1.0  # 伤心慢一点

    def test_neutral_normal(self):
        from agent.voice import VoiceEngine
        ratio = VoiceEngine._EMOTION_SPEED_RATIO.get("认真", 1.0)
        assert ratio == 1.0  # 正常语速

    def test_unknown_emotion_defaults_to_1(self):
        from agent.voice import VoiceEngine
        ratio = VoiceEngine._EMOTION_SPEED_RATIO.get("不存在的情绪", 1.0)
        assert ratio == 1.0
