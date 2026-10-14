"""基础行为场景：误触发 / 叫名字 / 窗口对话 / 互聊不每条回（2026-08-11）"""


def 注册(s):
    s.场景("误触发-女仆装", 误触发_女仆装, 组="基础行为")
    s.场景("误触发-猫娘图", 误触发_猫娘图, 组="基础行为")
    s.场景("叫名字-糖糖早安", 叫名字, 组="基础行为")
    s.场景("窗口-进入与接话", 窗口对话, 组="基础行为")
    s.场景("互聊-不每条回", 互聊, 组="基础行为")


def 误触发_女仆装(ctx):
    """「女仆装」不应被当叫名字"""
    ctx.send_group("女仆装真好看")
    silent = ctx.wait_silent()
    ctx.check("「女仆装」未被当叫名字", silent,
              "插话/窗口回复合法——只要不是'被提到了名字'")


def 误触发_猫娘图(ctx):
    """「猫娘图」不应被当叫名字"""
    ctx.send_group("发个猫娘图")
    silent = ctx.wait_silent()
    ctx.check("「猫娘图」未被当叫名字", silent)


def 叫名字(ctx):
    """正常叫名字应回复"""
    ctx.send_group("糖糖早安")
    r = ctx.wait_reply(want_text=True)
    ctx.check("「糖糖早安」糖糖回复", r is not None, (r or {}).get("reply") or "")
    if r:
        ctx.record_sample("糖糖早安", r.get("reply") or "", r.get("reason") or "")


def 窗口对话(ctx):
    """@糖糖进入窗口；糖糖在等回答时回应应接话"""
    ctx.send_group("糖糖在吗")
    r1 = ctx.wait_reply(want_text=True)
    ctx.check("@名字后进入对话", r1 is not None, (r1 or {}).get("reply") or "")
    if r1:
        waiting = any(q in (r1.get("reply") or "") for q in "？?吗呢")
        ctx.send_group("在呀，怎么了")
        r2 = ctx.wait_reply()
        if waiting:
            ctx.check("糖糖在等回答 → 回应接话", r2 is not None)
            if r2:
                ctx.record_sample("在呀，怎么了", r2.get("reply") or "", r2.get("reason") or "")
        else:
            ctx.check("糖糖未在等回答 → 不强制接话（正确行为）", True,
                      f"糖糖: {(r2 or {}).get('reason') or '未回复'}")


def 互聊(ctx):
    """群友互聊（无@）不应被当叫名字——插话/窗口接话合法"""
    reasons = []
    for msg in ["今天天气不错", "是啊，适合出门"]:
        ctx.send_group(msg)
        r = ctx.wait_reply()
        if r:
            reasons.append(r.get("reason") or "")
            ctx.record_sample(msg, r.get("reply") or "", r.get("reason") or "")
    name_triggered = any("被提到了名字" in x or "被@" in x for x in reasons)
    ctx.check("互聊未被当叫名字", not name_triggered, f"触发原因: {reasons}")
