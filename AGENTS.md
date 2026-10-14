# 小糖糖 — Codex 接手指南

> **先读 [CLAUDE.md](CLAUDE.md)**——思维方式和 35 条踩坑教训都在那里，本文件不重复（两处同步必然漂移，这是 2026-08-15 瘦身确立的规矩）。
> 本文件是 Codex 的入口（Codex 启动时自动读取），只保留两样 Codex 独有内容：**审查协议**（第五节）和**操作手册**（环境/验证/工具）。
> 冲突时以 CLAUDE.md 为准；数字一律以 CLAUDE.md 和实际运行结果为准，不在此硬写。

## 一、项目是什么

QQ 机器人「小糖糖」——LLM 人格 bot，**不是客服机器人**。她的存在感来自围绕 LLM 构建的持久状态结构（记忆/情绪/关系/驱动力），不是单次 `f(消息)→回复` 调用。

```
QQ消息 → SnowLuma(NapCat) → WebSocket → agent/handler.py → LLM → 回复
```

**核心原则（不可动摇）**：单脑架构——LLM 是唯一决策者，系统只提供能力（Native Tool Calling），不替 LLM 做判断。凡是「`if "xxx" in text` 关键词门控做行为分流」「系统抢答 LLM 该做的决定」都是历史反模式，见到即视为 bug（教训 #9/#10/#24）。关键文件角色速查见 CLAUDE.md 第三节。

## 二、环境（与常见 Linux 项目不同，别按惯性操作）

- **Windows 10**，shell 是 **Git Bash**（不是 cmd/PowerShell）；Python 3.10 在 PATH
- **本仓是 git 仓库**（2026-10-15 起初始化并推送 `downtown20/tangtang-qq-bot`）——正常 commit/push；旧发布仓 `D:\小糖糖-发布` 已弃用。GitNexus 刷新仍按 CLAUDE.md 第八节（带 `GITNEXUS_MAX_FILE_SIZE` 与 `--skip-git`）
- 数据：SQLite `memory.db`（唯一访问层是 store.py）；日志 `日志/tangtang.log` 极大（数十万行），**用 python 脚本过滤分析，不要直接 grep 全文**
- 运行时状态在 `状态/`（14 个 json）、日志在 `日志/`——落盘位置唯一入口是 `agent/paths.py` 的 `state_path()` / `log_path()`；**不许自己拼根相对路径**（2026-10-15 T55-F）
- 改代码后 **bot 需重启**才生效；破坏性 boolean 配置开关必须启动时显式警告（教训 #11）
- 批量审查/长任务建议 `codex exec --skip-git-repo-check -s workspace-write < prompt.txt`

## 三、改代码前必读（铁律摘要，完整 35 条在 CLAUDE.md 第二节）

1. **Read before Edit**——old_string 必须来自最近一次 Read；大段替换用 Edit 工具逐字匹配，**禁 heredoc 正则盲改**（教训 #25 曾误删整个类致 bot 启动即崩）
2. **最小改动**——一行能解决不改十行；修 3 个 bug 还没好 → 停下找共同根源（主要矛盾），不追单点
3. **先取证再下结论**——发现 bug 必须附证据之一：日志行/DB 查询结果/运行时探针输出/会红的测试；没有证据的发现降级为「待验证假设」，不得进 Critical/Important
4. **改任何函数前跑 GitNexus `impact`**——查直接调用方和受影响流程，HIGH/CRITICAL 风险必须显式报告；提交前 `detect_changes()` 核对影响面
5. **角色禁区**：role_card.md 只写身份不加规则（有机器闸门测试）；行为规则/协议进 protocols.py；**改 config.yaml 必须同步 糖糖控制台_qt.py 预设值**（教训 #4）
6. **禁止关键词替 LLM 决策**；中文文本匹配用 jieba 完整词检查，不用子串（`"想你" in "不要想你"` 永远为 True）
7. **Python 缩进即逻辑**——`return` 之后的代码要审查；方法定义只插在方法边界处（教训 #16/#28）

## 四、验证命令（每次改动后必跑，缺一不可）

```bash
python -m pytest tests/ -q          # 只跑 tests/ 目录（根目录有其他环境相关的收集错误）；必须全绿
python tools/静态检查.py    # 静态检查必须清零（不要手写 pyflakes agent/*.py ——
                            # pwsh 不展开 glob，会静默漏扫 80/82 个文件，见 CLAUDE.md）
```

两条都过了才算完。另注意：**测试绿 ≠ 路径被覆盖**——删代码/加分支后补对应路径的回归测试（教训 #25/#28）。功能改动最后要模拟场景端到端走一遍。

## 五、Codex 审查协议（本项目独有，审查任务必守）

1. **只读审查**：审查任务不修改任何代码；报告落盘 `docs/审查/Codex审查_<日期>.md`，会话最后输出 300 字内核心结论（Request Changes / Approved 判定 + 必须修的问题清单）。
2. **证据分级**：每条发现按 `位置(file:line) → 证据（哪条日志/探针输出/会红的测试）→ 根因 → 最小修复建议` 四段式；无证据的发现降级为「待验证假设」，不得进 Critical/Important。
3. **分级标准**：Critical（必须修）/ Important（应该修）/ Minor（可选）；效率优化建议必须附**测量证据**（耗时/内存/token 前后对比），没有测量的优化不算数。
4. **共因优先**：多个 bug 先问共同根源——修根，不修表。
5. 审查维度示例：并发/异步安全、权限与安全、持久化铁律（重启语义）、跨模块契约（新字段/新参数的**所有**消费者）、状态机边界、数据安全（脚本幂等性/误伤面）、测试质量（假绿检测）。

## 六、工具清单（2026-08-25 已配好，重启会话生效）

- **MCP 三台**：`gitnexus`（代码知识图——impact/query/context/detect_changes/rename）、`github`、`anysearch`（工具名格式 `mcp__<server>__<tool>`）
- **Skills**（`~/.codex/skills`，与 Claude Code 镜像同步，见 tools/同步skills.py）：**evidence-review（本项目审查纪律与输出格式，接手必读）**、gitnexus-* 系列（gitnexus-guide 有完整工具参考）、reverse 等；项目级技能在 `.codex/skills/`
- **Agents**（`~/.codex/agents`）：minimal-change-engineer、evidence-collector、reality-checker、incident-response-commander、codebase-archaeologist、codebase-onboarding-engineer、prompt-engineer、mcp-builder——复杂任务可点名调用
- **同步纪律**：改/装技能后跑 `python tools/同步skills.py`；MCP token 轮换后跑 `python tools/同步codex_mcp.py`；配置或技能变更后重启 Codex 会话才生效

## 七、接手第一步（按顺序）

1. 读 [CLAUDE.md](CLAUDE.md) 全文（思维方式 + 教训表 + 关键文件表）
2. 读 skills 里的 evidence-review（审查协议与输出格式）
3. `python tools/体检.py`——环境健康检查
4. `python -m pytest tests/ -q`——建立测试基线
5. 看 `docs/审查/` 下最新文档和项目记忆（`<用户目录>\.claude\projects\d--qq----\memory\MEMORY.md`）——了解进行中事项与最近事故

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
