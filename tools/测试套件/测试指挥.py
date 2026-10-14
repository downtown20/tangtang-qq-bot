"""
测试指挥.py —— 糖糖测试聊天 Agent（2026-08-11）

终端对话式控制测试：自然语言指令 → GPT 理解意图 → 执行 → 汇报。

用法：
  python tools/测试套件/测试指挥.py

指令示例：
  "测一下隐私场景"        → 跑 记忆与隐私 场景组
  "跑全部回归"            → 全部脚本场景
  "跑个加班的对话"        → 真实对话剧本「加班到只剩外卖盒」
  "换个话题，聊暗恋"      → 切到「那个人是不是也有点喜欢我」
  "跑个轻松点的"          → 随机挑一个轻松剧本
  "看上次的报告"          → 显示最近一次测试报告
  "有哪些剧本"            → 剧本列表
  "退出"                  → 结束
"""

import json
import random
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

import 套件 as 套件模块


def _codex(prompt: str, timeout: int = 120) -> str:
    """调 codex 取文本（stdin 传 prompt——Windows 参数会破坏中文换行）"""
    try:
        r = subprocess.run(
            ["codex.cmd", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check",
             "-c", 'model_reasoning_effort="low"'],
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


def 解析意图(指令: str, 剧本s: list[str]) -> dict:
    """GPT 理解用户指令 → 测试操作"""
    prompt = (
        "你是糖糖测试指挥的意图解析器。用户说了一句测试指令，判断要执行什么。\n"
        f"可用剧本：{json.dumps(剧本s, ensure_ascii=False)}\n"
        f"用户指令：{指令}\n\n"
        "输出 JSON（只输出 JSON，不要其他文字）：\n"
        '{"action": "回归|真实对话|报告|剧本列表|退出|闲聊", "target": ""}\n'
        "规则：\n"
        "- 提到 测/回归/场景/隐私/记忆/误触发/命令 等 → 回归，target 填场景组名（基础行为/记忆与隐私/命令与工具/全部）\n"
        "- 提到 对话/聊/剧本/话题/扮演 等 → 真实对话，target 填最匹配的剧本名；找不到就填 随便\n"
        "- 提到 随便/随机/自己加/你定/随便聊聊 等（用户懒得指定话题）→ 真实对话，target 填 随便\n"
        "- 提到 报告/结果/上次 → 报告\n"
        "- 提到 剧本/有哪些 → 剧本列表\n"
        "- 退出/结束/再见 → 退出\n"
        "- 闲聊/打招呼/别的 → 闲聊（target 填一句友好的简短回应）"
    )
    结果 = _codex(prompt)
    try:
        start = 结果.find("{")
        end = 结果.rfind("}")
        return json.loads(结果[start:end + 1])
    except Exception:
        return {"action": "闲聊", "target": "没听明白，再说一次？比如「测隐私」或「跑个加班的对话」"}


def 跑回归(ctx, 组: str = "全部"):
    """跑脚本场景回归"""
    ctx.results.clear()
    ctx.samples.clear()  # 防跨指令累积（否则报告越跑越臃肿、串剧）
    套件模块.重置测试号亲密()  # 每轮测试前净化：清窗口历史+亲密——防串戏/对话熟悉度失真
    s = 套件模块.套件(ctx)
    套件模块.加载场景库(s)
    if 组 == "全部":
        names = list(s.场景s)
        # 隐私前置（互动最少时测）
        原序 = {n: i for i, n in enumerate(names)}
        names.sort(key=lambda n: (0 if "隐私" in n else 1, 原序[n]))
    else:
        names = s.组s.get(组, [])
        if not names:
            return f"❌ 没有场景组「{组}」，可用: {list(s.组s)}"
    print(f"  🔄 执行回归（{组}，{len(names)} 场景）...")
    s.运行(names)
    ok = sum(1 for r in ctx.results if r["ok"])
    报告文件 = 套件模块.生成报告(ctx)
    return f"回归完成: {ok}/{len(ctx.results)} PASS —— 📄 {报告文件.name}"


def 跑真实对话(ctx, 剧本名: str, 轮数: int = 6):
    """跑真实对话（GPT 扮演浪浪）"""
    ctx.results.clear()
    ctx.samples.clear()  # 防跨指令累积
    套件模块.重置测试号亲密()  # 每轮测试前净化——清窗口历史+亲密，防串戏/熟悉度失真
    try:
        剧本 = 套件模块.加载剧本(剧本名)
    except SystemExit as e:
        return str(e)
    if not 剧本名:
        return f"可用剧本: {[p['name'] for p in 套件模块.加载剧本()]}"
    print(f"  🔄 真实对话「{剧本['name']}」（{轮数} 轮，约 {轮数 * 1.5} 分钟）...")
    套件模块.真实对话(ctx, 剧本, 轮数=轮数)
    结论 = ctx.results[-1]["detail"] if ctx.results else ""
    报告文件 = 套件模块.生成报告(ctx, 结论)
    return f"✅ 对话完成。审查: {结论[:120]}\n   📄 报告: {报告文件.name}"


def 显示报告():
    """显示最近一次测试报告"""
    报告目录 = BASE_DIR / "报告"
    files = sorted(报告目录.glob("测试报告-*.md"))
    if not files:
        return "还没有测试报告"
    最近 = files[-1]
    content = 最近.read_text(encoding="utf-8")
    return f"📄 {最近.name}\n\n" + content[:800]


def main():
    print("🕹️ 糖糖测试指挥已启动（输入指令，'退出'结束）")
    print("   示例: 测隐私 / 跑全部回归 / 跑个加班的对话 / 换个话题聊暗恋 / 看报告 / 有哪些剧本")
    ctx = 套件模块.Ctx("1092609076")
    剧本s = [p["name"] for p in 套件模块.加载剧本()]

    while True:
        try:
            指令 = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 退出")
            break
        if not 指令:
            continue

        意图 = 解析意图(指令, 剧本s)
        action = 意图.get("action", "闲聊")
        target = 意图.get("target", "")

        if action == "退出":
            print("👋 退出")
            break
        elif action == "回归":
            print(跑回归(ctx, target or "全部"))
        elif action == "真实对话":
            if not target or target == "随便":
                target = random.choice(剧本s)
                print(f"  🎲 随机挑了一个剧本: {target}")
            print(跑真实对话(ctx, target, 轮数=6))
        elif action == "报告":
            print(显示报告())
        elif action == "剧本列表":
            for p in 剧本s:
                print(f"  - {p}")
        else:
            print(f"  💬 {target}")


if __name__ == "__main__":
    main()
