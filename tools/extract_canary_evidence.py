"""按 boot/cid 提取脱敏的多模态 canary 日志证据。

默认只输出脱敏文本：用于协作报告时，不暴露真实 boot/cid、QQ 类 ID、
绝对路径，以及 ASR/识图的自由文本。过滤总在脱敏之前用原始 token 完成。
本工具不访问数据库，也不与 bot、NapCat 或网络交互。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Iterable
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent.paths import log_path  # noqa: E402  日志落盘位置单一事实源（2026-10-15 T55-F）


CONTEXT_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[+-]\d{4})?)\s+\|\s*"
    r"boot=(?P<boot>[^\s|]+)\s+cid=(?P<cid>[^\s|]+)\s*\|"
)
WINDOW_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[+-]\d{4})?$"
)
PATH_RE = re.compile(r"(?:file:///|file://)?[A-Za-z]:[\\/][^\s\]|,]+")
FILE_URI_RE = re.compile(r"file:///[^\s\]|,]+", re.IGNORECASE)
CQ_RE = re.compile(r"\[CQ:[^\]]+\]", re.IGNORECASE)
LONG_ID_RE = re.compile(r"(?<!\d)\d{5,20}(?!\d)")
KEYED_ID_RE = re.compile(
    r"\b(?P<key>qq|user|group|target|scope|message_id|action_id|source_id)="
    r"(?P<value>[^\s,|)]+)",
    re.IGNORECASE,
)
PAREN_ID_RE = re.compile(r"\((?:[^()]{0,30})?\d{5,20}\)")
SENSITIVE_TAIL_RE = re.compile(r"(🎤 ASR →|🖼 按需识图:)\s*.*")


def _alias(kind: str, value: str) -> str:
    """生成稳定短别名，允许同一摘录内关联而不暴露原值。"""
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]
    return f"<{kind}:{digest}>"


def _redact_message(message: str) -> str:
    """保留事件类别与状态，删除可能含聊天内容或定位信息的载荷。"""
    message = FILE_URI_RE.sub("<path>", message)
    message = PATH_RE.sub("<path>", message)
    message = CQ_RE.sub("[CQ:<redacted>]", message)
    message = SENSITIVE_TAIL_RE.sub(r"\1 <content:redacted>", message)
    message = KEYED_ID_RE.sub(
        lambda match: f"{match.group('key')}={_alias('id', match.group('value'))}",
        message,
    )
    message = PAREN_ID_RE.sub("(<id:redacted>)", message)
    return LONG_ID_RE.sub(lambda match: _alias("id", match.group(0)), message)


def redact_line(line: str) -> str:
    """脱敏一条标准日志行；非标准行只保留安全替换后的文本。"""
    match = CONTEXT_RE.match(line)
    if not match:
        return _redact_message(line)
    prefix = (
        f"{match.group('timestamp')} | boot={_alias('boot', match.group('boot'))} "
        f"cid={_alias('cid', match.group('cid'))} |"
    )
    return prefix + _redact_message(line[match.end():])


def select_lines(lines: Iterable[str], *, boot: str = "", cid: str = "",
                 after: str = "", before: str = "") -> list[str]:
    """以原始 boot/cid 和可比较的日志时间串选择标准日志行。"""
    selected: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        match = CONTEXT_RE.match(line)
        if not match:
            continue
        timestamp = match.group("timestamp")[:19]
        if after and timestamp < after[:19]:
            continue
        if before and timestamp > before[:19]:
            continue
        if boot and match.group("boot") != boot:
            continue
        if cid and match.group("cid") != cid:
            continue
        selected.append(line)
    return selected


def render(lines: Iterable[str], *, boot: str = "", cid: str = "",
           after: str = "", before: str = "") -> str:
    """渲染可直接附到协作报告的脱敏摘录。"""
    selected = select_lines(lines, boot=boot, cid=cid, after=after, before=before)
    filters = []
    if boot:
        filters.append(f"boot={_alias('boot', boot)}")
    if cid:
        filters.append(f"cid={_alias('cid', cid)}")
    if after:
        filters.append(f"after={after}")
    if before:
        filters.append(f"before={before}")
    header = [
        "# H6 Canary log evidence (redacted)",
        "filters: " + ", ".join(filters),
        f"matched_lines: {len(selected)}",
        "",
    ]
    return "\n".join(header + [redact_line(line) for line in selected]) + "\n"


def _validate_window(value: str, option: str) -> str:
    if value and not WINDOW_TIMESTAMP_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"{option} must be YYYY-MM-DD HH:MM:SS (timezone suffix optional)"
        )
    return value


def run_self_test() -> None:
    """内置无 I/O 样例：验证过滤先于脱敏、常见敏感值不会泄漏。"""
    raw = [
        "2026-09-03 10:00:00+0800 | boot=liveboot cid=evt-42 | 糖糖.ASR | INFO | 🎤 ASR → 我的号码是13312345678",
        "2026-09-03 10:00:01+0800 | boot=other cid=evt-42 | 糖糖.Handler | INFO | [CQ:image,file=file:///D:/secret/a.png]",
    ]
    result = render(raw, boot="liveboot", cid="evt-42")
    assert "matched_lines: 1" in result
    assert "liveboot" not in result and "evt-42" not in result
    assert "13312345678" not in result and "secret" not in result
    assert "<content:redacted>" in result
    assert len(select_lines(
        raw, boot="liveboot", cid="evt-42",
        after="2026-09-03 10:00:00+0800",
        before="2026-09-03 10:00:00+0800",
    )) == 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=log_path(),
                        help="UTF-8 log file (default: 日志/tangtang.log)")
    parser.add_argument("--boot", default="", help="exact original boot token")
    parser.add_argument("--cid", default="", help="exact original correlation id")
    parser.add_argument("--after", default="", type=lambda value: _validate_window(value, "--after"))
    parser.add_argument("--before", default="", type=lambda value: _validate_window(value, "--before"))
    parser.add_argument("--output", type=Path, help="new output file; refuses overwrite")
    parser.add_argument("--self-test", action="store_true", help="run deterministic redaction checks")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test()
        print("PASS: extract_canary_evidence self-test")
        return 0
    if not (args.boot or args.cid):
        parser.error("at least one of --boot or --cid is required to prevent broad extraction")
    if not args.log.is_file():
        parser.error(f"log file not found: {args.log}")
    if args.output and args.output.exists():
        parser.error(f"refusing to overwrite existing output: {args.output}")

    with args.log.open("r", encoding="utf-8", errors="replace") as lines:
        output = render(lines, boot=args.boot, cid=args.cid,
                        after=args.after, before=args.before)
    if "matched_lines: 0" in output:
        print(output, end="", file=sys.stderr)
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(f"wrote redacted evidence: {args.output}")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
