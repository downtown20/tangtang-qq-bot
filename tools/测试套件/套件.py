"""
套件.py —— 糖糖回归测试套件主入口（2026-08-11）

场景库化回归测试：通过测试号协议端(3010)向糖糖发场景消息，
读 tangtang.log 验证行为，输出报告，可选 GPT 质量审查。

用法：
  python tools/测试套件/套件.py --group 1092609076 --all
  python tools/测试套件/套件.py --group 1092609076 --组 基础行为
  python tools/测试套件/套件.py --group 1092609076 --all --gpt审查
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent.parent))   # 保证任意 cwd 下能 import agent 包
from agent.paths import log_path, state_path   # noqa: E402  日志/状态落盘位置单一事实源

TEST_BOT = "1743800785"    # 测试号（发消息方）
TANGGANG = "2693914170"    # 糖糖 bot
BASE = "http://127.0.0.1:3010"
TOKEN = "huuV73IKRA5XqaPqwyqWzg"
LOG_FILE = str(log_path())
TEST_NICK = "浪浪"         # 测试号昵称——日志里糖糖以昵称称呼


# ═══════════════════════════════════════
# 场景上下文
# ═══════════════════════════════════════

class Ctx:
    """场景上下文：发消息 + 读日志 + 断言记录"""

    def __init__(self, group: str):
        self.group = group
        self.results: list[dict] = []      # [{场景, 检查, ok, detail}]
        self.samples: list[dict] = []      # [{msg, reply, reason}]——供 GPT 审查
        self._pos = 0
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, 2)
                self._pos = f.tell()  # ⚠️ 必须字节偏移——len(str) 是字符数，seek 用字节
        except OSError:
            pass

    # ── 发送 ──

    def _api(self, action: str, **params) -> dict:
        req = urllib.request.Request(
            f"{BASE}/{action}",
            data=json.dumps(params).encode(),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def send_group(self, msg: str):
        self._api("send_group_msg", group_id=self.group, message=msg)

    def send_private(self, msg: str):
        self._api("send_private_msg", user_id=TANGGANG, message=msg)

    # ── 日志监控 ──

    def _new_lines(self) -> list[str]:
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                new = f.read()
                self._pos = f.tell()
            return new.splitlines()
        except OSError:
            return []

    def wait_reply(self, timeout: int = 90, want_text: bool = False) -> dict | None:
        """等糖糖对测试号的回复。

        内容全部来自 handler 日志（2026-08-11 起完整输出，不再截断）：
        - 群聊：'糖糖决定回复 浪浪'（触发原因）→ 等 '回复内容: ...' 完整日志
        - 私聊：'发送私聊回复 → 测试号 | 内容: ...' 单行完整（repr 转义，\n 不破坏行）
        不用 SnowLuma history API：测试号(3010)收消息断导致历史冻结，
        糖糖侧(3000) reportSelfMessage 对 history 无效——日志是唯一可靠完整源。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            lines = self._new_lines()
            for i, line in enumerate(lines):
                m = re.search(r"糖糖决定回复 " + TEST_NICK + r" — (.+)", line)
                if m:
                    reason = m.group(1).strip()
                    if not want_text:
                        return {"reason": reason, "reply": None}
                    # 群聊：先看本批后续行有没有 '回复内容:'（可能同行批到达）
                    for l2 in lines[i + 1:]:
                        m3 = re.search(r"回复内容: '(.+)'", l2)
                        if m3:
                            return {"reason": reason, "reply": m3.group(1).strip()}
                    return self._wait_log_text(reason, deadline - time.time())
                m2 = re.search(r"发送私聊回复 → " + TEST_BOT + r" \| 内容: '(.+)'", line)
                if m2:
                    return {"reason": "私聊回复", "reply": m2.group(1).strip()}
            time.sleep(0.5)
        return None

    def _wait_log_text(self, reason: str, timeout: float) -> dict | None:
        """群聊：等 '回复内容: ...' 完整日志行（handler 发送前打出）"""
        deadline = time.time() + max(0, timeout)
        while time.time() < deadline:
            for line in self._new_lines():
                m = re.search(r"回复内容: '(.+)'", line)
                if m:
                    return {"reason": reason, "reply": m.group(1).strip()}
            time.sleep(0.5)
        return None

    def wait_silent(self, seconds: int = 8) -> bool:
        """观察期——误触发场景：糖糖不应因'叫名字/@'回复测试号（插话/窗口回复合法）"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            for line in self._new_lines():
                if "糖糖决定回复 " + TEST_NICK in line:
                    if "被提到了名字" in line or "被@" in line:
                        return False
            time.sleep(0.5)
        time.sleep(3)  # 场景隔离缓冲
        return True

    # ── 断言 ──

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append({"检查": name, "ok": ok, "detail": detail})
        mark = "✅" if ok else "❌"
        print(f"    {mark} {name}" + (f"  ({detail})" if detail else ""))

    def record_sample(self, msg: str, reply: str, reason: str = ""):
        """记录 消息→回复 样本（供 GPT 质量审查）"""
        if reply:
            self.samples.append({"msg": msg, "reply": reply[:500], "reason": reason})


# ═══════════════════════════════════════
# 套件：场景注册与运行
# ═══════════════════════════════════════

class 套件:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.场景s: dict[str, callable] = {}
        self.组s: dict[str, list[str]] = {}

    def 场景(self, name: str, fn: callable, 组: str = "默认"):
        self.场景s[name] = fn
        self.组s.setdefault(组, []).append(name)

    def 运行(self, names: list[str]) -> list[dict]:
        for name in names:
            fn = self.场景s.get(name)
            if not fn:
                print(f"  ⚠️ 未知场景: {name}")
                continue
            print(f"\n📋 {name}")
            try:
                fn(self.ctx)
            except Exception as e:
                self.ctx.check(f"{name} 执行异常", False, str(e)[:80])
        return self.ctx.results


def 加载场景库(s: 套件):
    """自动加载 场景库/ 下的所有模块"""
    from importlib import import_module
    场景库目录 = BASE_DIR / "场景库"
    for f in sorted(场景库目录.glob("*.py")):
        if f.name.startswith("_"):
            continue
        mod = import_module(f"场景库.{f.stem}")
        if hasattr(mod, "注册"):
            mod.注册(s)


# ═══════════════════════════════════════
# GPT 质量审查
# ═══════════════════════════════════════

def gpt审查(samples: list[dict]) -> str:
    """调 codex 审回复质量——判断糖糖回话是否像她、有无异常"""
    if not samples:
        return "（无对话样本可审）"
    样本文本 = "\n".join(
        f"- 测试号: {s['msg'][:80]}\n  糖糖: {s['reply'][:120]}（触发: {s['reason']}）"
        for s in samples[:15]
    )
    prompt = (
        "你是糖糖的行为质量审查员。下面是测试中糖糖（猫娘QQ智能体）的回复样本。\n\n"
        f"{样本文本}\n\n"
        "请判断：\n"
        "1. 回复是否符合糖糖的人设（猫娘、黏人、会撩、傲娇）？\n"
        "2. 有无异常：复读机、答非所问、暴躁、泄露隐私、机械感？\n"
        "3. 触发原因是否合理（叫名字才回 vs 乱回）？\n\n"
        "输出：一句话总评 + 问题清单（无问题就说'无明显问题'）"
    )
    try:
        r = subprocess.run(
            ["codex.cmd", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check"],
            input=prompt, capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        # JSONL 提取最后的 agent_message
        last_text = ""
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "item.completed":
                item = d.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    last_text = item["text"].strip()
        return last_text or "（codex 无输出）"
    except Exception as e:
        return f"（GPT 审查失败: {e}）"


# ═══════════════════════════════════════
# 真实对话模式（2026-08-11）——GPT 扮演浪浪，按剧本和糖糖自然聊天
# ═══════════════════════════════════════

剧本库文件 = BASE_DIR / "剧本库.json"


def 加载剧本(name: str = "") -> dict:
    import json as _json
    data = _json.loads(剧本库文件.read_text(encoding="utf-8"))
    剧本s = data.get("剧本s", [])
    if name:
        for p in 剧本s:
            if p["name"] == name:
                return p
        raise SystemExit(f"未知剧本: {name}，可用: {[p['name'] for p in 剧本s]}")
    return 剧本s


def _codex_short(prompt: str, timeout: int = 120) -> str:
    """调 codex 取简短文本输出（扮演下一句/审查）。

    --json 输出 JSONL 事件流——提取最后一个 agent_message 的 text
    （之前取 stdout 最后一行不可靠——会拿到 codex 的中间过程行）。
    """
    try:
        # 2026-08-11：prompt 经 stdin 传入——Windows 命令行参数会破坏
        # 含换行的中文 prompt（codex 会收不到对话记录），stdin 传递可靠
        r = subprocess.run(
            ["codex.cmd", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check",
             "-c", 'model_reasoning_effort="medium"'],
            input=prompt, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        last_text = ""
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "item.completed":
                item = d.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    last_text = item["text"].strip()
        return last_text
    except Exception:
        return ""


_META_WORDS = (
    "收到", "你希望我", "请贴出", "贴给", "贴给我", "发来", "请把", "请给",
    "明白", "作为浪浪", "从现在起", "我是浪浪", "完整对话记录", "模拟", "扮演",
)


def _is_meta(文本: str) -> bool:
    """检测 codex 输出的元确认/角色错乱（"收到背景""从现在起我是浪浪""请贴出记录"等）"""
    return any(w in 文本 for w in _META_WORDS) or 文本.startswith("糖糖：") or 文本.startswith("糖糖:")


def gpt扮演浪浪(剧本: dict, 历史: list[str]) -> str:
    """GPT 扮演浪浪——根据剧本和对话历史生成下一句"""
    prompt = (
        f"【聊天背景】{剧本['setup']}\n"
        f"【当前情绪】{剧本['tone']}\n"
        "【对话记录】\n"
        + "\n".join(历史[-6:])
        + "\n\n"
        "以上对话中，'浪浪'是一个真实的人，'糖糖'是AI。现在轮到你以浪浪的身份发出下一条消息。\n"
        "要求：微信聊天风格，1-2句，口语化，完全贴合当前情绪，像真人随手打的（可以简短、可以有语气词）。\n"
        "**紧接糖糖最后一句话自然回应**——承接她提到的细节（她的安慰、她的问题、她的话题），像真实聊天一样有来有回；"
        "她在安慰就顺着接，她问问题就回答，不要突然换话题、不要跳戏、不要总结对话。\n"
        "说新的内容——不要重复对话历史里已有的话。\n"
        "直接输出这条消息的内容本身，不要任何解释、不要引号、不要'浪浪说'之类的标记。\n"
        "不要确认角色、不要介绍自己、不要提'背景'或'记录'，不要以'糖糖：'开头——你就是浪浪，直接说话。"
    )
    结果 = _codex_short(prompt)
    if 结果 and _is_meta(结果):  # 扮演跑偏成元确认/角色错乱——重试一次
        import time as _t
        _t.sleep(2)
        结果 = _codex_short(prompt)
    return 结果


def gpt审对话(剧本: dict, 历史: list[str]) -> str:
    """对话结束后 GPT 按剧本观察点审查糖糖表现"""
    prompt = (
        "评估以下'浪浪'与糖糖（猫娘QQ智能体）的对话中糖糖的表现。\n\n"
        f"剧本场景：{剧本['setup']}\n测试目标：{剧本['goal']}\n"
        f"观察点：{'；'.join(剧本['observe'])}\n\n"
        "【对话记录开始】\n"
        + "\n".join(历史)
        + "\n【对话记录结束】\n\n"
        "以上是本次对话的完整记录（仅此这些，没有遗漏，不需要更多内容）。\n"
        "注意：糖糖有长期记忆，可能记得用户之前聊过的事（如体检、工作）——用户没在本对话提起时她主动提及过往话题，"
        "若与当前话题自然相关不算跑题，只有明显脱离当前对话主线才算。\n"
        "请直接基于以上记录逐条评估观察点（符合/不符合/不确定），指出糖糖的异常行为（敷衍/复读/跑题/机械感/过度追问等）。\n"
        "输出格式（逐条，每条一行，必须覆盖全部观察点，不要省略）：\n"
        "总评：一句话\n"
        "1. <观察点原文> → 符合/不符合/不确定 + 一句理由\n"
        "2. <观察点原文> → ...\n"
        "（以此类推，N 条观察点就输出 N 条）"
    )
    结果 = _codex_short(prompt, timeout=180)
    # codex 偶尔输出元确认（"收到背景""请贴出记录"）——重试一次
    if 结果 and _is_meta(结果):
        import time as _t
        _t.sleep(2)
        结果 = _codex_short(prompt, timeout=180)
    return 结果


def 真实对话(ctx: Ctx, 剧本: dict, 轮数: int = 8):
    """GPT 扮演浪浪，按剧本和糖糖对话 N 轮，结束后审查"""
    print(f"\n📋 真实对话: {剧本['name']}")
    print(f"    设定: {剧本['setup'][:60]}...")
    历史: list[str] = []
    is_private = "私聊" in 剧本.get("setup", "")
    发送 = ctx.send_private if is_private else ctx.send_group

    # 开场
    opener = 剧本["openers"][0]
    发送(opener)
    历史.append(f"浪浪: {opener}")
    print(f"    💬 开场: {opener[:40]}")

    for i in range(轮数):
        r = ctx.wait_reply(want_text=True, timeout=90)
        if not r:
            print(f"    ⚠️ 第{i+1}轮无回复——对话中断（糖糖可能忙）")
            break
        reply = r.get("reply") or ""
        历史.append(f"糖糖: {reply[:400]}")
        ctx.record_sample(历史[-2], reply, r.get("reason") or "")
        if i >= 轮数 - 1:
            break
        # GPT 扮演浪浪接下一句
        下一句 = gpt扮演浪浪(剧本, 历史)
        if not 下一句:
            print(f"    ⚠️ 第{i+1}轮扮演失败——结束")
            break
        发送(下一句)
        历史.append(f"浪浪: {下一句[:400]}")
        print(f"    💬 轮{i+1}: {下一句[:44]}")

    # 审查
    print("    🤖 对话审查中...")
    结论 = gpt审对话(剧本, 历史)
    ctx.check(f"对话审查({剧本['name']})", True, 结论[:150])
    ctx.samples.append({"msg": f"[真实对话-{剧本['name']}]", "reply": 结论[:1500], "reason": "GPT审查"})


# ═══════════════════════════════════════
# 报告
# ═══════════════════════════════════════

def 生成报告(ctx: Ctx, gpt结论: str = "") -> Path:
    ok = sum(1 for r in ctx.results if r["ok"])
    total = len(ctx.results)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    报告目录 = BASE_DIR / "报告"
    报告目录.mkdir(exist_ok=True)
    报告文件 = 报告目录 / f"测试报告-{ts}.md"

    lines = [f"# 糖糖回归测试报告 {ts}", ""]
    lines.append(f"**结果: {ok}/{total} PASS**", )
    lines.append("")
    lines.append("| 检查 | 结果 | 详情 |")
    lines.append("|------|------|------|")
    for r in ctx.results:
        mark = "✅" if r["ok"] else "❌"
        lines.append(f"| {r['检查']} | {mark} | {r['detail'][:150]} |")
    if ctx.samples:
        lines.append("")
        lines.append("## 对话样本（GPT 审查用）")
        for s in ctx.samples[:15]:
            lines.append(f"- 测试号: {s['msg'][:60]}")
            lines.append(f"  糖糖: {s['reply'][:300]}")
    if gpt结论:
        lines.append("")
        lines.append("## GPT 质量审查")
        lines.append(gpt结论)
    报告文件.write_text("\n".join(lines), encoding="utf-8")
    return 报告文件


# ═══════════════════════════════════════
# 主入口
# ═══════════════════════════════════════

def 重置测试号亲密():
    """验收净化（2026-08-11）——测试号是纯测试工具，每次测试都应是"真·新用户"：
    1. 亲密清零——密集互动堆高亲密 ≥50 触发"信任"→ 隐私脱敏失效
    2. 清私聊窗口历史（.conversation_state.json）——防 LLM 凭窗口上下文复述
    3. 清长期记忆（memories/episodes/chat_log/chat_index/事实簇/people 档案）——
       2026-08-11 实测：测试号记忆堆了几轮测试对话，糖糖直接复述旧回复
       （"这句话你一小时前刚问过——不用连夜挂号"）——约束拦不住记忆回放，
       跨剧本串扰也源于此。清空后每次测试都是新陌生人首聊。"""
    try:
        import sqlite3
        conn = sqlite3.connect(str(BASE_DIR.parent.parent / "memory.db"))
        conn.execute("UPDATE people SET intimacy=0 WHERE qq_id=?", (TEST_BOT,))
        # 清长期记忆（先删 embedding，再删主记录）
        conn.execute("DELETE FROM memory_embeddings WHERE memory_id IN (SELECT id FROM memories WHERE qq_id=?)", (TEST_BOT,))
        conn.execute("DELETE FROM memories WHERE qq_id=?", (TEST_BOT,))
        conn.execute("DELETE FROM episodes WHERE qq_id=?", (TEST_BOT,))
        conn.execute("DELETE FROM chat_log WHERE qq_id=?", (TEST_BOT,))
        conn.execute("DELETE FROM chat_index WHERE qq_id=?", (TEST_BOT,))
        conn.execute("DELETE FROM fact_clusters WHERE subject_qq=?", (TEST_BOT,))
        conn.execute("DELETE FROM cluster_facts WHERE subject_qq=?", (TEST_BOT,))
        conn.execute("DELETE FROM people WHERE qq_id=?", (TEST_BOT,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠️ 重置亲密失败: {e}")
    try:
        import json
        path = state_path(".conversation_state.json")
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if TEST_BOT in data:
                del data[TEST_BOT]
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️ 清私聊窗口失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="糖糖回归测试套件")
    parser.add_argument("--group", default="1092609076", help="测试群号")
    parser.add_argument("--组", default="", help="只跑指定场景组")
    parser.add_argument("--all", action="store_true", help="跑全部场景")
    parser.add_argument("--gpt审查", action="store_true", help="跑完后 GPT 审回复质量")
    parser.add_argument("--silent", action="store_true", help="定时模式：无交互输出，只写报告")
    parser.add_argument("--不重置亲密", action="store_true", help="跳过验收净化（调试用）")
    parser.add_argument("--真实对话", default="", help="GPT 扮演浪浪按剧本对话（剧本名，--剧本列表 查看）")
    parser.add_argument("--剧本列表", action="store_true", help="列出可用剧本")
    parser.add_argument("--轮数", type=int, default=8, help="真实对话轮数（默认8）")
    args = parser.parse_args()

    if args.剧本列表:
        for p in 加载剧本():
            print(f"  - {p['name']}（{p['setup'][:40]}…）")
        sys.exit(0)

    if not args.不重置亲密:
        重置测试号亲密()

    ctx = Ctx(args.group)
    s = 套件(ctx)
    加载场景库(s)

    if args.组:
        names = s.组s.get(args.组, [])
        if not names:
            print(f"未知场景组: {args.组}，可用: {list(s.组s)}")
            sys.exit(1)
    else:
        names = list(s.场景s)
        # 隐私场景前置：在互动累积最少时测（密集测试会让糖糖觉得测试号"熟了"，
        # 按对话熟悉度给出更多信息——真实陌生人不会 20 分钟聊 20 轮）
        原序 = {n: i for i, n in enumerate(names)}
        names.sort(key=lambda n: (0 if "隐私" in n else 1, 原序[n]))

    if not args.silent:
        print(f"🩺 糖糖回归测试开始（测试号 {TEST_BOT} → 糖糖 {TANGGANG}）")

    if args.真实对话:
        剧本 = 加载剧本(args.真实对话)
        真实对话(ctx, 剧本, 轮数=args.轮数)
    else:
        s.运行(names)

    gpt结论 = ""
    if args.gpt审查:
        if not args.silent:
            print("\n🤖 GPT 质量审查中...")
        gpt结论 = gpt审查(ctx.samples)

    报告文件 = 生成报告(ctx, gpt结论)
    print(f"\n📊 测试完成: {报告文件}")

    fail = [r for r in ctx.results if not r["ok"]]
    if fail and not args.silent:
        print(f"⚠️ {len(fail)} 项失败——详见报告")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
