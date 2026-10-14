"""LLM 调优配置契约（2026-09-22）。

## 为什么有这三个键

判据是**「它是否真的改变性能或运行方式」**——不是"官方 API 有的都塞进控制台"。
逐个过官方 Chat Completions 文档后：

  · `tool_loop_limit`  一轮最多调几轮工具（原硬编码 3）—— 决定"一轮能做多少事"
  · `request_timeout`  单次请求超时秒数（原硬编码 120）—— 思考强度 max 时容易撞线
  · `user_id`          官方的调度/KVCache 隔离标识（原来没有）—— 双机共用 key 时有用

**明确没加的**（写在这里免得以后有人"顺手补上"）：
  · `top_p` —— 官方：思考模式有效区间**只有 0.95–1.0**，非思考模式**固定 1.0
    且忽略传入值**。加了等于没加，正是本仓最讨厌的「看着能调其实调不动」。
  · `frequency_penalty` / `presence_penalty` —— 官方**已弃用，传了不生效**。
  · `stop` / `response_format` —— 不改变性能或运行方式。

三个解析函数都遵守同一条纪律：**配置写错不该让 bot 起不来**——非法值一律
退回默认，不抛异常。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# request_timeout
# ---------------------------------------------------------------------------

def rt(cfg):
    from agent.handler import _request_timeout
    return _request_timeout(cfg)


@pytest.mark.parametrize("cfg", [None, {}, {"request_timeout": None},
                                 {"request_timeout": ""}, {"request_timeout": "abc"},
                                 {"request_timeout": []}])
def test_timeout_defaults_when_missing_or_garbage(cfg):
    assert rt(cfg) == 120.0


def test_timeout_reads_value():
    assert rt({"request_timeout": 300}) == 300.0
    assert rt({"request_timeout": "45"}) == 45.0


@pytest.mark.parametrize("bad", [0, -1, -999, "0", 1, 2, 4.9])
def test_silly_timeouts_fall_back_to_default_not_to_a_floor(bad):
    """低于 5 秒**退回默认 120**，不是截断成 5。

    一个 5 秒的超时不是"更快"，是让每一次请求都失败——填错值的人想要的是
    能跑，不是精确执行他的笔误。

    （本条是本仓自测抓出来的：原实现用 `max(5.0, ...)`，于是 `0` 走
    `0 or 120` 落到 120、而 `-1` 被截断成 5——同一个"无意义的值"两种结果。）
    """
    assert rt({"request_timeout": bad}) == 120.0


@pytest.mark.parametrize("seconds", [5, 5.0, 30, 300])
def test_timeout_at_or_above_floor_is_respected(seconds):
    assert rt({"request_timeout": seconds}) == float(seconds)


# ---------------------------------------------------------------------------
# user_id
# ---------------------------------------------------------------------------

def uid(cfg):
    from agent.handler import _user_id_value
    return _user_id_value(cfg)


@pytest.mark.parametrize("cfg", [None, {}, {"user_id": None}, {"user_id": ""}])
def test_user_id_empty_by_default(cfg):
    """默认不发这个字段——不能凭空给所有人加一个隔离标识。"""
    assert uid(cfg) == ""


def test_user_id_passthrough():
    assert uid({"user_id": "desktop-01"}) == "desktop-01"
    assert uid({"user_id": "a_b-C9"}) == "a_b-C9"


def test_user_id_strips_illegal_chars_instead_of_400():
    """官方允许字符集是 [a-zA-Z0-9-_]。非法字符**剔除**而不是原样发出去——
    一个写错的隔离标识不该让每一轮对话都挂（官方会回 400）。"""
    assert uid({"user_id": "台式机"}) == ""            # 全非法 -> 空 -> 不发
    assert uid({"user_id": "desktop 01"}) == "desktop01"
    assert uid({"user_id": "a.b/c@d"}) == "abcd"
    assert uid({"user_id": "中文desktop"}) == "desktop"


def test_user_id_truncated_to_512():
    assert len(uid({"user_id": "x" * 900})) == 512


# ---------------------------------------------------------------------------
# 明确不加的那些 —— 用测试把"不加"的理由钉住
# ---------------------------------------------------------------------------

def test_no_top_p_knob():
    """官方：非思考模式下 top_p 固定 1.0、传入值被忽略；思考模式下有效区间只有
    0.95–1.0。一个只能从 0.95 调到 1.0 的旋钮不该占控制台一格。"""
    import agent.handler as h
    src = Path(h.__file__).read_text(encoding="utf-8", errors="replace")
    assert '"top_p"' not in src, "top_p 被加回来了——先看本文件顶部的理由"


@pytest.mark.parametrize("deprecated", ["frequency_penalty", "presence_penalty"])
def test_no_deprecated_penalty_knobs(deprecated):
    """官方明确标注这两个已弃用、传了不生效。加了就是误导。"""
    import agent.handler as h
    src = Path(h.__file__).read_text(encoding="utf-8", errors="replace")
    assert f'"{deprecated}"' not in src, f"{deprecated} 已弃用，别加"
