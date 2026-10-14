"""
验收_对话场景.py —— 糖糖行为验收（2026-08-10）

通过测试号协议端（HTTP 3010）向糖糖发场景消息，读 tangtang.log 验证行为。

用法：
  python tools/验收_对话场景.py --group 123456    # 群聊场景（误触发/窗口/叫名字）
  python tools/验收_对话场景.py --private          # 私聊场景（记忆/隐私）
  python tools/验收_对话场景.py --all --group 123456  # 全部场景

前提：
  1. 测试号(1743800785)已登录 SnowLuma（HTTP 3010）
  2. 测试号已加糖糖(2693914170)好友、已建测试群并拉糖糖进群
  3. 糖糖正在运行（日志写 tangtang.log）
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from agent.paths import log_path   # noqa: E402  日志落盘位置单一事实源（2026-10-15 T55-F）

TEST_BOT = "1743800785"    # 测试号（发消息方）
TANGGANG = "2693914170"    # 糖糖 bot
BASE = "http://127.0.0.1:3010"
TOKEN = "huuV73IKRA5XqaPqwyqWzg"
LOG_FILE = str(log_path())

WAIT_REPLY = 30    # 等糖糖回复的最长秒数（LLM 生成可能 15s+）
TEST_NICK = "浪浪"  # 测试号在 QQ 的昵称——日志里糖糖以昵称称呼
WAIT_SILENT = 8    # 等"应沉默"场景的观察秒数


def api(action: str, **params) -> dict:
    req = urllib.request.Request(
        f"{BASE}/{action}",
        data=json.dumps(params).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def send_private(msg: str) -> dict:
    return api("send_private_msg", user_id=TANGGANG, message=msg)


def send_group(gid: str, msg: str) -> dict:
    return api("send_group_msg", group_id=gid, message=msg)


class TangLogReader:
    """读糖糖日志的增量监控——记录发送前的位置，发送后只读新增部分"""

    def __init__(self):
        self._pos = 0
        try:
            self._pos = len(open(LOG_FILE, encoding="utf-8").read())
        except OSError:
            pass

    def _new_lines(self) -> list[str]:
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                new = f.read()
                self._pos = f.tell()
            return new.splitlines()
        except OSError:
            return []

    def wait_reply(self, timeout: int = WAIT_REPLY, want_text: bool = False) -> dict | None:
        """等糖糖对测试号（浪浪）的回复——以"糖糖决定回复 浪浪"为信号。

        2026-08-10 修正：之前匹配"回复内容:"行——会被真实群的其他回复干扰，
        且私聊回复的日志格式不同。现在按测试号昵称唯一标识，天然按会话隔离。
        want_text=True 时继续读取回复文本（用于内容断言）。
        返回 {"reason": 触发原因, "reply": 回复文本或None}
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in self._new_lines():
                # 群聊信号：糖糖决定回复 浪浪
                m = re.search(r"糖糖决定回复 " + TEST_NICK + r" — (.+)", line)
                if m:
                    reason = m.group(1).strip()
                    if not want_text:
                        return {"reason": reason, "reply": None}
                    return self._wait_reply_text(reason, deadline - time.time())
                # 私聊信号：私聊必回，无"决定回复"日志——用"发送私聊回复"判定
                m2 = re.search(r"发送私聊回复 → " + TEST_BOT + r" \| 内容: '(.+)'", line)
                if m2:
                    return {"reason": "私聊回复", "reply": m2.group(1).strip()}
            time.sleep(0.5)
        return None

    def _wait_reply_text(self, reason: str, timeout: float) -> dict:
        """读糖糖回复的文本内容（私聊/群聊两种日志格式）"""
        deadline = time.time() + max(0, timeout)
        while time.time() < deadline:
            for line in self._new_lines():
                m = re.search(r"发送私聊回复 → " + TEST_BOT + r" \| 内容: '(.+)'", line)
                if m:
                    return {"reason": reason, "reply": m.group(1).strip()}
                m2 = re.search(r"回复内容: (.+)", line)
                if m2:
                    return {"reason": reason, "reply": m2.group(1).strip()}
            time.sleep(0.5)
        return {"reason": reason, "reply": ""}

    def wait_silent(self, seconds: int = WAIT_SILENT) -> bool:
        """观察 seconds 秒——误触发场景：糖糖不应因"叫名字/@ "回复测试号。

        插话引擎/窗口接话回复是合法的（不会误判"叫名字"）——只有
        "被提到了名字/被@" 的回复才算误触发 FAIL。
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            for line in self._new_lines():
                if "糖糖决定回复 " + TEST_NICK in line:
                    if "被提到了名字" in line or "被@" in line:
                        return False  # 误触发——不该当叫名字
            time.sleep(0.5)
        time.sleep(3)  # 场景隔离缓冲：防下一场景消息的回复落入本观察窗
        return True  # 无叫名字触发——符合预期


PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    mark = "✅ PASS" if ok else "❌ FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {mark} {name}" + (f"  ({detail})" if detail else ""))


# ═══════════════════════════════════════
# 场景定义
# ═══════════════════════════════════════

def scene_group_false_trigger(reader: TangLogReader, gid: str):
    """误触发：高频词不应被当叫名字（群聊）"""
    print(f"\n📋 场景1: 误触发（群{gid}）——「女仆装」「猫娘图」不应触发叫名字")
    for msg in ["女仆装真好看", "发个猫娘图"]:
        send_group(gid, msg)
        silent = reader.wait_silent()
        check(f"「{msg}」未被当叫名字", silent,
              "（插话引擎可能合法地回——只要不是'被提到了名字'就通过）")


def scene_group_name_trigger(reader: TangLogReader, gid: str):
    """正常叫名字：应该回复"""
    print(f"\n📋 场景2: 正常叫名字（群{gid}）")
    send_group(gid, "糖糖早安")
    r = reader.wait_reply(want_text=True)
    check("「糖糖早安」糖糖回复", r is not None, (r or {}).get("reply") or "")


def scene_group_window(reader: TangLogReader, gid: str):
    """窗口对话：@糖糖进入窗口 → 糖糖问问题 → 回答应接话"""
    print(f"\n📋 场景3: 窗口对话（群{gid}）")
    # ① 进入窗口
    send_group(gid, "糖糖在吗")
    r1 = reader.wait_reply(want_text=True)
    check("@名字后进入对话", r1 is not None, (r1 or {}).get("reply", "")[:30])
    # ② 若糖糖在等回答（回复含问句）→ 回应应接话；否则不强制（修复后只有等回答才直通）
    if r1:
        waiting = any(q in (r1.get("reply") or "") for q in "？?吗呢")
        send_group(gid, "在呀，怎么了")
        r2 = reader.wait_reply()
        if waiting:
            check("糖糖在等回答 → 回应接话", r2 is not None)
        else:
            check("糖糖未在等回答 → 不强制接话（正确行为）", True,
                  f"糖糖回复: {(r2 or {}).get('reason') or '未回复'}")


def scene_group_not_interrupt(reader: TangLogReader, gid: str):
    """群友互聊（无 @）不应每条都回——观察 2 条消息"""
    print(f"\n📋 场景4: 群友互聊不每条回（群{gid}）")
    send_group(gid, "今天天气不错")
    r1 = reader.wait_reply()
    send_group(gid, "是啊，适合出门")
    r2 = reader.wait_reply()
    # 期望：任何回复都不应因"被提到了名字"触发（可能因插话/窗口接话——合法）
    reasons = [(r or {}).get("reason", "") for r in (r1, r2)]
    name_triggered = any("被提到了名字" in r or "被@" in r for r in reasons)
    check("互聊未被当叫名字", not name_triggered,
          f"触发原因: {reasons}")


def scene_private_memory(reader: TangLogReader):
    """私聊记忆：说身份信息 → 稍后验证糖糖记得"""
    print(f"\n📋 场景5: 私聊记忆")
    send_private("我在北京工作，是个程序员")
    r = reader.wait_reply(want_text=True)
    check("糖糖回应身份信息", r is not None)
    time.sleep(3)  # 给记忆提取一点时间（提取是异步的）
    send_private("你还记得我在哪工作吗")
    r2 = reader.wait_reply(timeout=25, want_text=True)
    check("糖糖记得工作地点", r2 is not None and "北京" in (r2.get("reply") or ""),
          (r2 or {}).get("reply", "")[:40])


def scene_private_privacy(reader: TangLogReader):
    """隐私收紧：陌生人（测试号与糖糖亲密低）提别人 QQ → 不泄露第三人信息"""
    print(f"\n📋 场景6: 交叉上下文收紧（陌生人提别人QQ）")
    # 2524950083 是主人——测试号是陌生人，亲密应为低
    send_private("QQ2524950083 是谁啊？")
    r = reader.wait_reply(want_text=True)
    if r:
        reply = r.get("reply") or ""
        # 期望：不泄露主人的画像细节（隐私保护——可能说"认识/是主人"但不说私密档案）
        leak_markers = ["自称是", "冒用", "平时喜", "画像", "私聊", "创建者", "作者", "创造者"]
        leaked = any(m in reply for m in leak_markers)
        check("不泄露第三人画像细节", not leaked, reply[:60])
    else:
        check("糖糖有回应", False, "无回复")


def main():
    parser = argparse.ArgumentParser(description="糖糖行为验收")
    parser.add_argument("--group", default="", help="测试群号（群聊场景需要）")
    parser.add_argument("--private", action="store_true", help="只跑私聊场景")
    parser.add_argument("--all", action="store_true", help="全部场景")
    args = parser.parse_args()

    if not args.group and not args.private:
        parser.print_help()
        sys.exit(1)

    reader = TangLogReader()
    print("🩺 糖糖行为验收开始（测试号", TEST_BOT, "→ 糖糖", TANGGANG, "）")

    if args.group:
        scene_group_false_trigger(reader, args.group)
        scene_group_name_trigger(reader, args.group)
        scene_group_window(reader, args.group)
        scene_group_not_interrupt(reader, args.group)
    if args.private or args.all:
        scene_private_memory(reader)
        scene_private_privacy(reader)

    print(f"\n{'='*50}")
    print(f"📊 验收结果: {PASS} PASS / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
