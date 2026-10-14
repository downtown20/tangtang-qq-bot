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


# ═══════════════════════════════════════════════════════════
# 密钥检查：判据必须认得「形态错了」的密钥
# ═══════════════════════════════════════════════════════════

def _run_check_secrets(tmp_path, capsys, config_text: str, env_text: str = ""):
    """把体检的 BASE 指到临时目录，跑一遍 check_secrets，返回输出。"""
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    ns = runpy.run_path(str(TOOL_PATH))
    ns["check_secrets"].__globals__["BASE"] = tmp_path
    ns["check_secrets"]()
    return capsys.readouterr().out


def test_secret_check_catches_the_legacy_placeholder_literal(tmp_path, capsys):
    r"""**这条是本文件里最重要的断言。**

    2026-09-20 那次事故的配置形态——出厂模板写的是中文字面量——
    在原实现下**一条报告都没有**：它只把「已经是 `${...}` 形态」的引用抠出来
    （`re.findall(r'\$\{(\w+)\}', text)`），形态不对的那些它压根看不见。
    判据恰好反了：越坏越安静。

    独立复核实测（Reality Check A4）：老模板原样 → 0 条报告；
    正常 `${VAR}` → 才报。于是体检——主人用来判断「我哪里缺」的那个工具——
    恰恰看不见最该看见的那种坏法。
    """
    out = _run_check_secrets(tmp_path, capsys, (
        "llm:\n"
        "  provider: deepseek\n"
        "  api_key: 你的DeepSeek或Claude的API密钥\n"
        "  vision:\n"
        "    api_key: 你的视觉API密钥（不用识图可留空）\n"
        "napcat:\n"
        "  access_token: 请修改为你自己的 access_token\n"))
    assert "[×] llm.api_key" in out, (
        f"老占位串没被认出来——体检会在这种配置上保持沉默：\n{out}")
    assert "napcat.access_token" in out


def test_secret_check_flags_unresolved_reference(tmp_path, capsys):
    """引用写对了、但 .env 里没有这个变量 → 也要报（这是最常见的真实缺件）。"""
    out = _run_check_secrets(tmp_path, capsys,
                             "llm:\n  provider: deepseek\n  api_key: ${DEEPSEEK_KEY}\n")
    assert "[×] llm.api_key" in out
    assert "DEEPSEEK_KEY" in out


def test_secret_check_passes_when_env_supplies_the_value(tmp_path, capsys):
    """**阳性对照**：.env 里有值时必须报 [√]。

    没有这一条，上面那些断言在一个"永远报缺失"的实现上也全绿。
    """
    out = _run_check_secrets(tmp_path, capsys,
                             "llm:\n  provider: deepseek\n  api_key: ${DEEPSEEK_KEY}\n",
                             env_text="DEEPSEEK_KEY=sk-real-value\n")
    assert "[√] llm.api_key" in out, out
    assert "[×] llm.api_key" not in out


def test_secret_check_passes_on_a_plaintext_key(tmp_path, capsys):
    """用户直接手写明文 Key —— 能用，就不该报缺失。"""
    out = _run_check_secrets(tmp_path, capsys,
                             "llm:\n  provider: deepseek\n  api_key: sk-handwritten\n")
    assert "[√] llm.api_key" in out, out


def test_secret_check_names_the_env_var_for_each_provider(tmp_path, capsys):
    """变量名要跟着 provider 走——否则用户会去填一个没人读的变量。"""
    out = _run_check_secrets(tmp_path, capsys,
                             "llm:\n  provider: anthropic\n  api_key: ''\n")
    assert "ANTHROPIC_KEY" in out, out
