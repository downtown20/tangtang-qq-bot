"""闸门：`.gitignore` 必须「放行该入库的，挡住该隐藏的」。

为什么需要这条（2026-10-26 根目录规范化时连踩两个「改了但没生效」——教训 #53 那一家）：

1. **否定规则的位置**：`.env.example` 被第 11 行 `.env.*` 挡住，白名单 `!.env.example`
   必须写在排除规则**后面**才生效；写在前面等于没写。
2. **目录级排除吃掉否定规则**：原先写的是 `stickers/`（整个目录排除），此时
   `!stickers/README.md` 之类的 `!` 规则**完全不生效**——父目录被排除后 git 不再往里看
   （惰性否定）。正确写法是 `stickers/*` + `!stickers/*.md`。

判据不自己实现 gitignore 语义（自己实现 = 两套口径，必然漂移——教训 #51），而是把本仓
`.gitignore` **原样**复制进一个临时 git 仓库，让 **git 自己**给样本文件分类。

阳性对照（`test_negative_control_*`）：拿同一段文本删掉/改坏对应的规则，样本必须变成
「被忽略」——证明这条闸门真的会红，而不是永远绿（教训 #37：尺子必须能红）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = ROOT / ".gitignore"

# 必须能进仓库：新 clone 下来就该有的模板与说明（少了它们 = 主人说的「内容不全」）
MUST_KEEP = [
    "config.example.yaml",          # 配置模板（builder-bootstrap 新建）
    ".env.example",                 # 密钥模板；曾被 `.env.*` 挡掉
    "prompts/_groups.yaml",         # 协议分组唯一真相
    "prompts/thinking.md",          # 协议覆盖层，曾整目录被 `prompts/` 排除
    "songs/README.md",              # 摆放说明（builder-bootstrap 新建）
    "songs/audio/README.md",        # 换行证明 `songs/**/*.md` 也放行
    "stickers/README.md",           # 四个贴图目录的说明文件都要能进
    "stickers_cg/README.md",
    "stickers_michele/README.md",
    "stickers_murasame/README.md",
]

# 必须被忽略：密钥 / 运行时状态 / 大数据 / 媒体本体 / 内部实现脚本
MUST_IGNORE = [
    ".env",
    "config.yaml",
    "config.yaml.bak",
    "config.yaml.bak_20261026_223143",
    "memory.db",
    "jieba.cache",
    "voice.pcm",
    ".build_complete",
    "songs/audio/x.wav",              # 只放行四首成品 + *.md，别的音频不进来
    "stickers/a.jpg",                 # 媒体本体进库会把仓库撑爆
    "stickers_cg/_tag/tag_cg_ollama.py",  # 子目录实现仍被 `stickers_cg/*` 挡住
]

_SKIP = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git 才能校验 .gitignore 语义")


def _classify(tmp_path: Path, gitignore_text: str, relpaths: list[str]) -> tuple[set[str], set[str]]:
    """在临时 git 仓库里复制给定 .gitignore，返回 (未跟踪且未被忽略, 被忽略) 两组相对路径。"""
    repo = tmp_path / "probe"
    repo.mkdir()
    (repo / ".gitignore").write_bytes(gitignore_text.encode("utf-8"))
    for rel in relpaths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"probe\n")

    # 不用 `git add -n`：它对不存在的路径直接 fatal，且会把「目录被排除」折叠成一行。
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True, capture_output=True)
    out = subprocess.run(
        ["git", "status", "--porcelain", "-uall", "--ignored=matching"],
        cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    untracked: set[str] = set()
    ignored: set[str] = set()
    for line in out.stdout.splitlines():
        status, path = line[:2], line[3:]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if status == "??":
            untracked.add(path)
        elif status == "!!":
            ignored.add(path.rstrip("/"))
    return untracked, ignored


def _is_ignored(rel: str, ignored: set[str]) -> bool:
    """自身或任一祖先目录被忽略，就算被忽略（`-uall` 也仍可能折叠整个被排除的目录）。"""
    parts = Path(rel).parts
    return any("/".join(parts[: i + 1]) in ignored for i in range(len(parts)))


@pytest.fixture(scope="module")
def ignore_text() -> str:
    return GITIGNORE.read_text(encoding="utf-8")


@_SKIP
def test_templates_and_scaffold_notes_are_committable(tmp_path, ignore_text):
    """模板与说明文件必须能提交——否则 clone 下来的人看到的是一个「缺零件」的仓库。"""
    untracked, ignored = _classify(tmp_path, ignore_text, MUST_KEEP)
    blocked = [p for p in MUST_KEEP if _is_ignored(p, ignored)]
    assert not blocked, "这些文件被 .gitignore 挡掉了，应该能入库：" + repr(blocked)
    # 反向确认样本真的被 git 看见了（不是「没建出来」而白通过）
    assert {p for p in MUST_KEEP} <= {p for p in untracked} | {_parent_dir(p) for p in untracked}, (
        "样本没被 git 看见（-uall 下应逐个列出）"
    )


@_SKIP
def test_secrets_state_and_media_stay_ignored(tmp_path, ignore_text):
    """密钥、本地配置、数据库、缓存、媒体本体必须留在库外。"""
    _, ignored = _classify(tmp_path, ignore_text, MUST_IGNORE)
    leaked = [p for p in MUST_IGNORE if not _is_ignored(p, ignored)]
    assert not leaked, "这些文件本该被 .gitignore 挡住，却会被提交：" + repr(leaked)


@_SKIP
def test_negative_control_dropping_env_example_whitelist_hurts(tmp_path, ignore_text):
    """阳性对照 ①：删掉 `!.env.example`，`.env.example` 必须重新被 `.env.*` 吃掉。"""
    without = _drop_lines(ignore_text, ("!.env.example",))
    _, ignored = _classify(tmp_path, without, [".env.example"])
    assert _is_ignored(".env.example", ignored), "闸门失效：白名单删了竟然也判定为可入库"


@_SKIP
def test_negative_control_dir_level_exclusion_swallows_whitelist(tmp_path):
    """阳性对照 ②：合成一段「目录级排除 + 否定规则」，证明 git 的惰性否定确实会吞掉 `!`。

    这条不依赖本仓 `.gitignore` 的当前写法——它证明的是「为什么必须写 `stickers/*`」
    这个机制本身；真实文件是否写对由下一条守卫负责。
    """
    regressed = "stickers/\n!stickers/*.md\n"
    _, ignored = _classify(tmp_path, regressed, ["stickers/README.md", "stickers/a.jpg"])
    assert _is_ignored("stickers/README.md", ignored), "闸门失效：目录级排除竟没吞掉否定规则"
    assert _is_ignored("stickers/a.jpg", ignored)


@_SKIP
def test_gitignore_never_uses_dir_level_exclusion_for_media_dirs():
    """回归守卫：媒体目录必须写成 `目录/*` + `!目录/*.md`，不能退回目录级。

    退回目录级 `stickers/` 的后果是 `!stickers/*.md` 静默失效——说明文件进不了库，
    而 `.gitignore` 自己不会报错（教训 #53：改了但没生效）。
    """
    lines = {line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()}
    bad = [d for d in ("stickers/", "stickers_cg/", "stickers_michele/", "stickers_murasame/")
           if d in lines]
    assert not bad, "目录级排除会和 `!` 白名单失效：请写 `目录/*` + `!目录/*.md`，实际发现：" + repr(bad)


@_SKIP
def test_negative_control_prompts_dir_would_hide_override_layer(tmp_path, ignore_text):
    """阳性对照 ③：把 `prompts/` 加回去，协议覆盖层立刻整目录消失（历史事故的复现）。"""
    regressed = ignore_text + "\nprompts/\n"
    _, ignored = _classify(tmp_path, regressed, ["prompts/thinking.md", "prompts/_groups.yaml"])
    assert _is_ignored("prompts/thinking.md", ignored), "闸门失效：`prompts/` 排除没起作用"
    assert _is_ignored("prompts/_groups.yaml", ignored)


def _drop_lines(text: str, needles: tuple[str, ...]) -> str:
    kept = [line for line in text.splitlines(True) if not any(n in line for n in needles)]
    assert len(kept) < len(text.splitlines(True)), f"没删掉任何行：{needles}"
    return "".join(kept)


def _parent_dir(rel: str) -> str:
    parent = str(Path(rel).parent)
    return "" if parent == "." else parent
