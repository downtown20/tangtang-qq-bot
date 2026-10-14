"""
测试 2026年7月新功能：生日/定时/画图/播报/Markdown/入群欢迎/情绪语音/群管理
"""

import asyncio
import pytest
import tempfile, os
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


def _percent(rate: str) -> int:
    """edge-tts 的 rate 字符串 → 数字：'+5%' → 5 / '-8%' → -8 / '+0%' → 0。

    agent/voice.py 的 EMOTION_SPEED 用字符串表达语速（直接喂给 edge-tts），
    断言方向（快/慢/不变）就得先把它解析出来——别用字符串比较，
    '+5%' > '-8%' 成立靠的是字典序巧合，不是语义。
    """
    return int(rate.strip().rstrip("%"))


def _frozen_datetime(*args):
    """datetime 替身：now() 冻结在给定时刻，strptime 等继承真实实现。

    CronScheduler._parse_fire_at 也走模块里的 datetime，所以替身必须仍能 strptime。
    """
    from datetime import datetime as _real
    frozen = _real(*args)

    class _Frozen(_real):
        @classmethod
        def now(cls, tz=None):
            return frozen

    return _Frozen


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
    # ── M2（2026-10-12 审计 P2）：once 任务的 misfire ──

    def test_once_task_late_inside_grace_still_fires(self, sched, monkeypatch):
        """旧实现是 `fire_at == now.strftime(...)` 全等比较——错过那一分钟就
        永久丢失且无声；1 分钟的迟到（进程刚重启完）必须补发。"""
        import agent.scheduler as _sch

        sched._tasks = [{
            "id": 1, "type": "once", "text": "迟到一分钟",
            "fire_at": "2026-09-25 09:00", "group_id": "123",
        }]
        fired = []

        async def fake_fire(task):
            fired.append(task["id"])
            return "confirmed"

        sched._fire = fake_fire
        monkeypatch.setattr(_sch, "datetime", _frozen_datetime(2026, 9, 25, 9, 1, 5))
        asyncio.run(sched._tick())
        assert fired == [1]

    def test_once_task_in_the_future_does_not_fire(self, sched, monkeypatch):
        import agent.scheduler as _sch

        sched._tasks = [{
            "id": 2, "type": "once", "text": "还没到",
            "fire_at": "2026-09-25 09:10", "group_id": "123",
        }]
        fired = []

        async def fake_fire(task):
            fired.append(task["id"])
            return "confirmed"

        sched._fire = fake_fire
        monkeypatch.setattr(_sch, "datetime", _frozen_datetime(2026, 9, 25, 9, 5, 0))
        asyncio.run(sched._tick())
        assert fired == []
        assert "status" not in sched._tasks[0]

    def test_once_task_beyond_grace_is_marked_missed_not_fired(self, sched, monkeypatch):
        """超过窗口不补发（四小时前的「5分钟后叫我」补发是噪声），
        但终态要落盘——丢可以，静默丢不行。"""
        import agent.scheduler as _sch

        sched._tasks = [{
            "id": 7, "type": "once", "text": "过期了",
            "fire_at": "2026-09-25 09:00", "group_id": "123",
        }]
        fired = []

        async def fake_fire(task):
            fired.append(task["id"])
            return "confirmed"

        sched._fire = fake_fire
        monkeypatch.setattr(_sch, "datetime", _frozen_datetime(2026, 9, 25, 12, 0, 0))
        asyncio.run(sched._tick())
        assert fired == []
        assert sched._tasks[0]["status"] == "missed"

    def test_frozen_uncertain_once_task_is_never_refired(self, sched, monkeypatch):
        """铁律回归：uncertain 永不自动重放（OneBot 无幂等令牌，重放＝重复消息）。

        从「全等比较」改成「区间比较」后，若不跳过终态，冻结的 uncertain 会在
        每个 tick 被重新触发——这条就是那个陷阱的阳性对照。
        """
        import agent.scheduler as _sch

        sched._tasks = [{
            "id": 9, "type": "once", "text": "发送未确认",
            "fire_at": "2026-09-25 09:00", "group_id": "123",
            "status": "uncertain",
        }]
        fired = []

        async def fake_fire(task):
            fired.append(task["id"])
            return "confirmed"

        sched._fire = fake_fire
        monkeypatch.setattr(_sch, "datetime", _frozen_datetime(2026, 9, 25, 9, 5, 0))
        asyncio.run(sched._tick())
        assert fired == [], "冻结的 uncertain 任务被重放了"
        assert sched._tasks[0]["status"] == "uncertain"


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
    """情绪 → 语速的**存活**契约（2026-10-15 随 CosyVoice3 删除改写）。

    为什么改：VoiceEngine 里曾有一个类属性 `_EMOTION_SPEED_RATIO`（情绪 → 倍率），
    定义处的注释写明是「情绪 → CosyVoice speed 倍率」，唯一消费者是 `_cosy_tts`
    （取证：agent/voice.py.bak-20261014-T36:577-597）。CosyVoice3 整体删除后，它随
    消费者一起消失——原来那四条用例直接引用这个属性，于是整组报红，但它们测的是
    **已经不存在的 provider**。

    现在真正在跑的两条路径各有各的语速表达，这组用例改成钉住它们：
      · edge-tts   → 模块级 `EMOTION_SPEED`（情绪 → "+5%"/"-8%" 字符串，agent/voice.py:45）
      · GPT-SoVITS → LLM 显式传的 `speed` 倍率，由 `_gpt_sovits` 夹在
                     VOICE_SPEED_MIN..VOICE_SPEED_MAX（agent/voice.py:518）
    并顺手钉住「系统不替 LLM 从情绪推断语速」这条单脑原则（agent/voice.py:56 的原话：
    「系统只校验和执行，不从情绪替 LLM 推断」）。
    """

    def test_happy_is_faster(self):
        from agent.voice import EMOTION_SPEED
        assert _percent(EMOTION_SPEED["开心"]) > 0

    def test_sad_is_slower(self):
        from agent.voice import EMOTION_SPEED
        assert _percent(EMOTION_SPEED["伤心"]) < 0

    def test_neutral_is_unchanged(self):
        from agent.voice import EMOTION_SPEED
        assert _percent(EMOTION_SPEED["认真"]) == 0
        assert _percent(EMOTION_SPEED["温柔"]) == 0

    def test_whisper_is_slower(self):
        """「悄悄话」必须比正常慢——它是最容易被写反的一条。"""
        from agent.voice import EMOTION_SPEED
        assert _percent(EMOTION_SPEED["悄悄话"]) < 0

    def test_every_emotion_stays_within_a_sane_band(self):
        """情绪微调只在 ±10% 内：它是「微调」，不是变速播放。"""
        from agent.voice import EMOTION_SPEED
        out_of_band = {k: v for k, v in EMOTION_SPEED.items() if abs(_percent(v)) > 10}
        assert not out_of_band, f"情绪语速微调超出 ±10%：{out_of_band}"

    def test_unknown_emotion_defaults_to_no_change(self):
        """没见过的情绪标签 → 不改语速（edge-tts 路径的取法：get(..., "+0%")）。"""
        from agent.voice import EMOTION_SPEED
        assert _percent(EMOTION_SPEED.get("不存在的情绪", "+0%")) == 0

    def test_llm_speed_argument_is_bounded(self):
        """LLM 传的 speed 有界——上界放开等于允许她把语音播成快进。"""
        from agent.voice import VOICE_SPEED_MAX, VOICE_SPEED_MIN
        assert 0 < VOICE_SPEED_MIN < 1.0 < VOICE_SPEED_MAX
        assert VOICE_SPEED_MAX <= 2.0, "上界超过 2 倍速会让语音失真"
        clamp = lambda s: max(VOICE_SPEED_MIN, min(VOICE_SPEED_MAX, float(s)))
        assert clamp(9.9) == VOICE_SPEED_MAX
        assert clamp(0.01) == VOICE_SPEED_MIN
        assert clamp(1.0) == 1.0

    def test_system_does_not_infer_speed_from_emotion(self):
        """防复删：情绪 → 倍率的**自动推断**不许回来（单脑原则）。

        它原来是 CosyVoice 路径的专属表；若有人「顺手补回来」塞给 GPT-SoVITS，
        就成了「系统替 LLM 决定语速」——CLAUDE.md 教训 #9/#10 的反模式。
        真要引入，先让它出现在 `speed` 参数的默认值里说服这条闸门。
        """
        from agent.voice import VoiceEngine
        assert not hasattr(VoiceEngine, "_EMOTION_SPEED_RATIO")
