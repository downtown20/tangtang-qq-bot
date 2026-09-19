"""回归测试：小游戏技能必须容忍 LLM 工具调用传入的 JSON 字符串参数。"""

import pytest

from agent import games
from agent.skills import execute_skill


@pytest.fixture(autouse=True)
def _reset_game_state():
    games._GAME_STATE.clear()
    yield
    games._GAME_STATE.clear()


@pytest.mark.asyncio
async def test_guess_number_coerces_json_string_argument():
    """工具 schema 当前把参数编码为 string，不能因此让技能抛 TypeError。"""
    result = await execute_skill("guess_number", {"guess": "0"})

    assert result
    assert "技能执行失败" not in result
    assert "想好了" in result


@pytest.mark.asyncio
async def test_guess_number_rejects_invalid_string_without_exception():
    result = await execute_skill("guess_number", {"guess": "不是数字"})

    assert "技能执行失败" not in result
    assert "数字" in result


def test_guess_number_missing_context_uses_requested_key_and_cleans_it(monkeypatch):
    monkeypatch.setattr(games.random, "randint", lambda _low, _high: 42)

    assert "想好了" in games._do_guess(0, context_key="group-a")
    # 没有显式新开局时也必须回写同一上下文，不能落到共享的 current 键。
    assert "太低" in games._do_guess(41, context_key="group-a")
    assert "答对" in games._do_guess(42, context_key="group-a")
    assert "group-a" not in games._GAME_STATE
    assert "current" not in games._GAME_STATE

