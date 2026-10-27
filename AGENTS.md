# 小糖糖 — Codex 接手指南

> **先读 [CLAUDE.md](CLAUDE.md)**：思维方式、教训表（56 条）、关键文件表、铁律、
> 已确认有效的积累——都在那里。**本文件不重复任何一条**（两处同步必然漂移，
> 这是 2026-08-15 瘦身确立的规矩；2026-10-24 再瘦一次，删掉与 CLAUDE.md 重复的
> 项目简介/环境/铁律摘要/接手清单，只留 Codex 独有的三块）。
> 冲突时以 CLAUDE.md 为准；数字一律以实际运行结果为准，不在此硬写。

## 一、验证命令（每次改动必跑，缺一不可）

```bash
python -m pytest tests/ -q                  # 只跑 tests/（根目录有环境相关收集错误）；必须全绿
python tools/静态检查.py                     # undefined 必须 0；它会打印实际扫描的文件数
python tools/变异对照.py --spec 变异/变异.json  # 行为改动必跑；GREEN = 假变异 = 不合格（退出码 2）
```

三条都过才算完。**凡闸门/扫描器/判据类改动，必须自带阳性对照**（种一个真样本进去，
看它红不红——教训 #37）；**测试绿 ≠ 路径被覆盖**（删代码/加分支后补对应路径的
回归测试——教训 #25/#28）。功能改动最后要模拟场景端到端走一遍。

## 二、Codex 审查协议（本项目独有，审查任务必守）

1. **只读审查**：审查任务不修改任何代码；报告落盘 `docs/审查/Codex审查_<日期>.md`，
   会话最后输出 300 字内核心结论（Request Changes / Approved 判定 + 必须修的问题清单）。
2. **证据分级**：每条发现按 `位置(file:line) → 证据（哪条日志/探针输出/会红的测试）
   → 根因 → 最小修复建议` 四段式；无证据的发现降级为「待验证假设」，
   不得进 Critical/Important。
3. **分级标准**：Critical（必须修）/ Important（应该修）/ Minor（可选）；
   效率优化建议必须附**测量证据**（耗时/内存/token 前后对比），没有测量的优化不算数。
4. **共因优先**：多个 bug 先问共同根源——修根，不修表。
5. **审查维度**：并发/异步安全、权限与安全、持久化铁律（重启语义）、
   跨模块契约（新字段/新参数的**所有**消费者）、状态机边界、数据安全
   （脚本幂等性/误伤面）、测试质量（假绿检测）。

## 三、工具（完整清单见索引，不在此手抄）

完整清单：[docs/工作方式/工具索引_20261024.md](docs/工作方式/工具索引_20261024.md)
（5 台 MCP + 50 个 skill 分类 + 本地工具 + DSH 插件调研结论）。

必须记住的三条：

- **`gitnexus` 只用来发现，不用来证明**——教训 #36：它只解析约一半的调用边，
  报「0 调用方」不等于没有调用方，必须 grep/AST 复核；
- **`evidence-review` 是接手必读 skill**（本项目审查纪律与输出格式）；
- **上下文紧张时先用 `jev-filter`** 做相关性预筛，再把少量结果读进上下文。

## 四、接手第一步

0. **先读 [docs/交接/开工交接单_20261024.md](docs/交接/开工交接单_20261024.md)**
   ——当前进度、下一步任务、未完成的尾巴、环境、回滚命令、主人偏好与红线全在
   里面。**跳过它，你会重做已经做完的事**（四个回挂批次 + 画像重生都已落地）。
1. 读 [CLAUDE.md](CLAUDE.md) 全文（思维方式 + 教训表 + 关键文件表）
2. `python tools/体检.py` 建立环境基线 → `python -m pytest tests/ -q` 建立测试基线
3. 看 `docs/审查/` 最新一份 + [docs/决策/预测台账.md](docs/决策/预测台账.md)
   （进行中事项、最近判断与未回填的预测）

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **qq-小糖糖** (44482 symbols, 93045 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/qq-小糖糖/context` | Codebase overview, check index freshness |
| `gitnexus://repo/qq-小糖糖/clusters` | All functional areas |
| `gitnexus://repo/qq-小糖糖/processes` | All execution flows |
| `gitnexus://repo/qq-小糖糖/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
