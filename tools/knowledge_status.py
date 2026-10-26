# -*- coding: utf-8 -*-
"""知识库「两个通道」的现状（给桌面端看，不加载模型）。

## 为什么需要这个模块

主人 2026-10-26 问：「当在『检索命中』部分添加新的知识文档时，需要新建索引才有用，对吧？」

对，但只对**检索通道**有效。知识库实际上有两条互不相通的通道：

- **检索通道**：`knowledge/**` 里的 md/txt，进 `search_knowledge` 的倒排 + 向量索引。
  边界由 `agent/knowledge.py::discover_document_files` 决定——**它会排除敏感目录**。
- **场景触发通道**：`knowledge/<受保护包>/<数字前缀>/` 里的槽位，由
  `agent/intimate_reference.py` 整篇插进上下文，**永远不进检索索引**（安全设计），
  而且**只在对应模式激活时才注入**：私聊条件是「已挂该模式」且「该模式已激活」；
  群聊不看激活状态，改由该模式的 `allow_groups` 开关决定（默认关）。
  2026-10-26 修正：这里原先写「群聊永不挂载」，在 `allow_groups` 落地后已经不成立。

所以「磁盘上 71 个文件、索引里 56 个」不是丢了 15 篇，而是那 15 篇本来就该走另一条通道。
（这个模块第一版把第二通道叫「常驻直投 / 每轮都在」——**是错的**，
2026-10-26 主人纠正：「这会在我的『亲密模式』下才触发」。）

## 为什么不在界面里算

两处各算一次，必然出现「界面说 56、实际索引 55」这种对不上（教训 #38）。
边界（哪些目录算敏感）也只认 `agent/knowledge.py` 那份常量，这里不重写。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
INDEX_DB = KNOWLEDGE_DIR / ".knowledge_index.sqlite3"


def status() -> dict:
    """返回两个通道的现状 + 索引缺口。

    结构：
        {
          "retrieval": {disk, indexed, missing[], index_mtime, stale_docs},
          "pinned": {files, dirs},
          "sensitive_dirs": [...],
          "needs_sync": bool,
        }
    """
    # 边界只认生产代码那一份（这里 import 的是纯函数，不会加载模型）
    from agent.knowledge import SENSITIVE_DIRECTORY_NAMES, discover_document_files

    retrievable = discover_document_files(KNOWLEDGE_DIR)
    rel_paths = [
        p.relative_to(KNOWLEDGE_DIR).as_posix() for p in retrievable
    ]

    indexed: dict[str, str] = {}
    index_mtime = ""
    index_size = 0
    if INDEX_DB.is_file():
        index_size = INDEX_DB.stat().st_size
        index_mtime = datetime.fromtimestamp(INDEX_DB.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            con = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True)
            try:
                cur = con.cursor()
                cur.execute("SELECT relative_path, updated_at FROM documents")
                indexed = {r[0]: r[1] for r in cur.fetchall()}
            finally:
                con.close()
        except sqlite3.Error:
            # 索引库坏了不能拖垮整页：把「读不出来」如实报上去
            indexed = {}

    missing = [p for p in rel_paths if p not in indexed]
    orphan = [p for p in indexed if p not in set(rel_paths)]

    # 场景触发通道：数字前缀槽位（`discover_slot_files` 只认数字开头目录）
    pinned_files: list[str] = []
    pinned_dirs: list[str] = []
    try:
        from agent.intimate_reference import discover_slot_files, package_dir

        root = package_dir(KNOWLEDGE_DIR)
        for f in discover_slot_files(root):
            pinned_files.append(f.relative_to(root).as_posix())
        if root.is_dir():
            for slot in sorted(root.iterdir()):
                # 只有数字前缀目录进注入（`_原文/` 这类归档不算）
                if slot.is_dir() and slot.name[:1].isdigit():
                    pinned_dirs.append(slot.name)
    except Exception as exc:  # noqa: BLE001 - 直投通道读不出来时不影响检索通道的结论
        pinned_files = [f"（读取失败：{type(exc).__name__}）"]

    return {
        "retrieval": {
            "disk": len(rel_paths),
            "indexed": len(indexed),
            "missing": missing,
            "orphan": orphan,
            "index_mtime": index_mtime,
            "index_bytes": index_size,
            # 索引库读不出来（表结构变 / 文件损坏）
            "index_readable": bool(indexed) or not INDEX_DB.is_file(),
        },
        # 场景触发通道：不是「每轮都在」，是「那个模式激活了才在」
        "scenario_channel": {
            "files": len(pinned_files),
            "slots": pinned_dirs,
            # ⚠ 2026-10-26 修正：原先写「群聊永不挂载」——**已经不对了**。
            # 群聊路径现在由 `allow_groups` 决定（默认关，开了就注入，
            # 见 agent/handler.py 群场景分支）。文案只讲机制，不预设哪边不可能。
            "trigger": ("对应模式激活后才注入：私聊要「已挂该模式 + 已进入该模式」，"
                        "群聊还要该模式显式允许群聊"),
        },
        # 这些目录按设计不进检索索引。**界面默认只显示数量**——
        # 目录名是使用者的内容设定，不该摊在界面文案里（主人 2026-10-26）
        "protected_dirs": sorted(SENSITIVE_DIRECTORY_NAMES),
        # 有缺口就要重载——界面据此显示红色提示与「同步」按钮
        "needs_sync": bool(missing or orphan),
        "sync_hint": "在 QQ 里发 `/知识 重载`，或重启糖糖；索引是加载时同步的，不会自动跟文件变化",
    }
