# 恋爱军师 · 外部知识库

> **本目录内容来自上游仓库，未经改写，仅供 `search_knowledge` 检索。**

| 项 | 值 |
|---|---|
| 上游 | https://github.com/shengjidaguai-china/goutoujunshi |
| commit | `6db7354a4002dc7c448a9c87ffdad8132570c9d3` |
| 拉取日期 | 2026-10-14 |
| 许可 | MIT（见上游 LICENSE） |
| 来源目录 | `references/knowledge/` → `知识/`，`references/practical/` → `话术/` |
| 文件数 | 43（知识 20 + 话术 23）|

## 为什么放在这里（而不是别处）

上游是一个 **Codex Skill**，它是**给「军师」用的**——语气是「你应该先做 A 再做 B」。
小糖糖**不是顾问，她自己就是关系中的一方**（CLAUDE.md 第零节：她不是客服机器人）。

所以整合的边界是：

| 层 | 载体 | 是否被本目录影响 |
|---|---|---|
| **她是谁** | `role_card.md` | ❌ **不动**（有机器闸门）|
| **她怎么做事** | `protocols.py` | ⚠️ 只加「怎么看这些资料」的说明 |
| **她知道什么** | `knowledge/` | ✅ 就是这里 |

**这些是资料，不是她的口吻。** 检索到的是参考观点，不是必须转述的结论。

## 未收录的部分与理由

| 上游路径 | 为什么不收 |
|---|---|
| `scripts/memory_store.py` | 小糖糖有 `memory.py` + `store.py`。**两套记忆系统并存**是反模式 #8 的同形 |
| `documentation/` | 是上游自己的开发文档，与本项目无关，只会稀释检索 |
| `SKILL.md` / `agents/` / `assets/` | 上游的 Codex 契约与装饰资源，本项目的消费方不存在 |

## 上游更新时怎么同步

```bash
git clone --depth 1 https://github.com/shengjidaguai-china/goutoujunshi.git /tmp/goutou
git -C /tmp/goutou rev-parse HEAD          # 与本文件顶部的 commit 对比
diff -rq /tmp/goutou/references/knowledge 'knowledge/恋爱军师/知识'
```

**只覆盖变更的文件，保留本 README。** 若某篇被上游删除，本地也删（避免留下过期知识）。
