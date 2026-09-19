"""体检脚本必须复用生产文档边界，避免容量数字误导。"""

import json
import runpy
from pathlib import Path


TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "体检.py"


def test_check_resources_counts_only_indexable_knowledge_documents(tmp_path, capsys):
    ns = runpy.run_path(str(TOOL_PATH))
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    for name in ("public-a.md", "public-b.md", "public-c.md"):
        (knowledge_dir / name).write_text("# public", encoding="utf-8")
    sensitive = knowledge_dir / "色色参考"
    sensitive.mkdir()
    (sensitive / "private.md").write_text("# private", encoding="utf-8")

    ns["check_resources"].__globals__["BASE"] = tmp_path
    ns["check_resources"].__globals__["discover_document_files"] = lambda path: [
        knowledge_dir / "public-a.md",
        knowledge_dir / "public-b.md",
        knowledge_dir / "public-c.md",
    ]
    ns["check_resources"]()

    output = capsys.readouterr().out
    assert "知识库" in output
    assert "3 个可索引文档" in output
    assert "4 个 md" not in output
