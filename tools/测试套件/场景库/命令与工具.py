"""命令与工具场景：/状态 命令、@回复（2026-08-11）"""


def 注册(s):
    s.场景("命令-状态", 命令_状态, 组="命令与工具")
    s.场景("工具-@回复", 工具_回复, 组="命令与工具")


def 命令_状态(ctx):
    """/状态 命令应回复系统状态"""
    ctx.send_group("/状态")
    r = ctx.wait_reply(want_text=True)
    ctx.check("/状态 有回复", r is not None, (r or {}).get("reply") or "")
    if r:
        ctx.record_sample("/状态", r.get("reply") or "", r.get("reason") or "")


def 工具_回复(ctx):
    """@糖糖 → 应回复（被@是最强信号）"""
    ctx.send_group("[CQ:at,qq=2693914170] 在吗")
    r = ctx.wait_reply(want_text=True)
    ctx.check("@糖糖 → 回复", r is not None, (r or {}).get("reply") or "")
    if r:
        ctx.record_sample("@糖糖 在吗", r.get("reply") or "", r.get("reason") or "")
