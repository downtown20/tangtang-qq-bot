"""长期优化 · 快照采集器 📸

一次性采集本项目的"可度量状态"，落盘为 JSON，供长期优化任务做棘轮对比。

用法：
    python tools/长期优化快照.py            # 快速（不含 pytest，约 10 秒）
    python tools/长期优化快照.py --full     # 含全量 pytest（约 7 分钟，建议后台跑）
    python tools/长期优化快照.py --compare  # 与上一份快照对比并打印差异

设计约束：
    - 全程只读：memory.db 用 file:?mode=ro，不写任何生产文件
    - 不扫 tangtang.log 全文（数十万行）：只读尾部固定字节
    - 输出 UTF-8，落 docs/开发规划/长期优化/快照/
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.paths import log_path, state_path  # noqa: E402  日志/状态落盘位置单一事实源

OUT_DIR = ROOT / "docs" / "开发规划" / "长期优化" / "快照"

# 关键文件：改动必须同步进快照，行尾被改写要立刻发现（铁律 E3）
KEY_FILES = [
    "agent/handler.py",
    "agent/reply_pipeline.py",
    "agent/store.py",
    "agent/memory.py",
    "agent/protocols.py",
    "agent/vision_router.py",
    "agent/vision_local.py",
    "main.py",
    "糖糖控制台_qt.py",
    "config.yaml",
]


def _sha(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
        "crlf": data.count(b"\r\n"),
        "lf_total": data.count(b"\n"),
        "lineending": "CRLF" if data.count(b"\r\n") and data.count(b"\r\n") == data.count(b"\n")
        else ("LF" if data.count(b"\r\n") == 0 else "MIXED"),
    }


def collect_fingerprints() -> dict:
    out = {}
    for rel in KEY_FILES:
        p = ROOT / rel
        if p.exists():
            out[rel] = _sha(p)
    return out


def collect_scale() -> dict:
    """agent/ 规模：文件数、行数、def/class 数（AST）"""
    files = sorted((ROOT / "agent").glob("*.py"))
    total_lines = total_defs = total_classes = 0
    for f in files:
        src = f.read_text(encoding="utf-8", errors="ignore")
        total_lines += src.count("\n") + 1
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                total_defs += 1
            elif isinstance(node, ast.ClassDef):
                total_classes += 1
    return {
        "agent_files": len(files),
        "agent_lines": total_lines,
        "agent_defs": total_defs,
        "agent_classes": total_classes,
    }


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "[timeout]"
    except FileNotFoundError:
        return -2, "[not found]"


def collect_vulture() -> dict:
    """vulture 命中数（棘轮指标，只许降）。

    2026-10-14 T2.1 补充：带 `vulture_whitelist.py` 一起跑——它只声明**结构上必活**的
    名字（技能注册 / Qt 回调 / 实例属性 / 回滚 import），实测把 60 口径的 315 条压到 248。
    纪律：**不许把命中全写进白名单**——那会让 vulture 永久报 0，等于关掉闸门（教训 #30 变体）。
    改动白名单后必须跑阳性对照：种一个真死函数，它仍要被抓到。
    """
    wl = ROOT / "tools" / "vulture_whitelist.py"   # 2026-10-15 T55：白名单随诊断脚本移入 tools/
    args = [sys.executable, "-m", "vulture", "agent/", "--min-confidence", "90"]
    if wl.exists():
        args.insert(-2, str(wl))
    code, out = _run(args)
    hits = [l for l in out.splitlines() if re.match(r"^agent[\\/]", l.strip())]
    by_kind: dict[str, int] = {}
    for h in hits:
        kind = h.rsplit("(", 1)[-1].rstrip(")").strip() if "(" in h else "?"
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"total": len(hits), "by_kind": by_kind, "sample": hits[:15]}


def collect_radon() -> dict:
    code, out = _run([sys.executable, "-m", "radon", "cc", "agent/", "-j"])
    try:
        data = json.loads(out)
    except Exception:
        return {"error": "radon 输出无法解析", "cc_ge_25": None, "top": []}
    rows = []
    for f, items in data.items():
        if not isinstance(items, list):
            continue
        for it in items:
            cc = it.get("complexity", 0)
            if cc >= 25:
                rows.append({"cc": cc, "file": f.replace("\\", "/"), "line": it.get("lineno"), "name": it.get("name")})
    rows.sort(key=lambda x: -x["cc"])
    return {"cc_ge_25": len(rows), "top": rows[:15]}


def collect_pyflakes() -> dict:
    targets = ["agent"] + [d for d in ["onebot"] if (ROOT / d).exists()] + ["main.py", "糖糖控制台_qt.py"]
    files: list[str] = []
    for t in targets:
        p = ROOT / t
        if p.is_dir():
            files += [str(x) for x in p.glob("*.py")]
        elif p.exists():
            files.append(str(p))
    code, out = _run([sys.executable, "-m", "pyflakes"] + files)
    undefined = [l for l in out.splitlines() if "undefined" in l.lower()]
    return {"undefined": len(undefined), "all_messages": len([l for l in out.splitlines() if l.strip()]),
            "undefined_sample": undefined[:10]}


def collect_pytest(full: bool) -> dict:
    if not full:
        return {"skipped": True}
    code, out = _run([sys.executable, "-m", "pytest", "tests/", "-q"], timeout=1800)
    tail = "\n".join(out.strip().splitlines()[-6:])
    m = re.search(r"(\d+) passed", out)
    f = re.search(r"(\d+) failed", out)
    return {"exit_code": code, "passed": int(m.group(1)) if m else None,
            "failed": int(f.group(1)) if f else 0, "tail": tail}


def collect_db() -> dict:
    """只读打开 memory.db，采集关键表计数"""
    db = ROOT / "memory.db"
    if not db.exists():
        return {"error": "memory.db 不存在"}
    out: dict = {}
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        for t in ["chat_log", "memories", "cluster_facts", "people", "confirmed_action_facts",
                  "send_outbox", "tasks", "turn_actions"]:
            if t in tables:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    out[t] = cur.fetchone()[0]
                except Exception as e:
                    out[t] = f"err:{e}"
        conn.close()
    except Exception as e:
        return {"error": str(e)}
    return out


def collect_log_tail(kb: int = 400) -> dict:
    """只读日志尾部固定字节，抓关键信号（不扫全文）"""
    log = log_path()
    if not log.exists():
        return {"error": "tangtang.log 不存在"}
    size = log.stat().st_size
    with log.open("rb") as f:
        f.seek(max(0, size - kb * 1024))
        tail = f.read().decode("utf-8", errors="ignore")
    sigs = ["ERROR", "CRITICAL", "Traceback", "boot="]
    return {
        "size_mb": round(size / 1048576, 1),
        "mtime": datetime.fromtimestamp(log.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "signals": {s: tail.count(s) for s in sigs},
    }


# ── 对话质量观测（2026-10-14 台账 T17，维度 6）──
# 用户质量目标：「提升盲测真人误判率、回复满意度和回复有效信息占比」，并明确**不设数值**。
# 因此这里只做**只观测**——把分布打出来供人判断，不设阈值、不下结论、不告警。
#
# 数据源不是新建的：`reply_metrics` 表 + `store.record_reply_metric()` 早在 2026-08-17
# 就建好并在发送提交点接线（handler.py:13402-13403），表注释原话是「盲测/A-B 的观测基础」。
# 但它一直**没有读取方**——CLAUDE.md 教训 #12/#14 的同形（写了 infra 代码 ≠ 接入了业务）。
# T17 做的是给它接上读取方，不重复造轮子。
#
# 为什么不用 tangtang.log：实测日志只有发送回执（📤 私聊已发送 → qq (msg_id=…)），
# **不含回复正文**——从日志统计「模板味命中」只会得到全 0 的假数据（教训 #36）。

# 与 agent/self_check.py 的 _ANTI_TELL_PATTERNS 同义：assistant_tell 列即那 7 条助手腔/罐装镜头，
# bracket_action 列即括号动作镜头（2026-08-17 取证：真实分布里它占 11.8%，是模板味第一来源）。
_LEN_BUCKETS = [(30, '<=30'), (60, '31-60'), (100, '61-100'),
               (150, '101-150'), (250, '151-250'), (None, '>250')]


def collect_dialog_quality(days: int = 30) -> dict:
    """从 reply_metrics 表统计对话质量分布（只观测，不设目标）。

    采集：
    1. reply_len：长度分桶 + 均值/中位数（用户反馈「回复长度不稳定」的直接观测）
    2. bracket_action / assistant_tell / meow / question_end：模板味四特征命中率
    3. hard_turn：困难轮次占比
    4. by_type：群聊 vs 私聊分列（用户目标是「接近真人私聊」）

    默认取最近 days 天——近端数据才反映当前状态，历史全量会被旧版本污染。
    """
    db = ROOT / "memory.db"
    if not db.exists():
        return {"error": "memory.db 不存在"}
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reply_metrics'")
        if not cur.fetchone():
            conn.close()
            return {"error": "reply_metrics 表不存在（迁移未跑？）"}

        cur.execute(
            "SELECT reply_len, meow, bracket_action, question_end, assistant_tell, "
            "hard_turn, target_type FROM reply_metrics "
            "WHERE ts >= datetime('now','localtime',?)",
            (f"-{int(days)} days",),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return {"error": str(e)}

    if not rows:
        return {"window_days": days, "samples": 0, "note": "窗口内无数据"}

    lens = sorted(int(r[0] or 0) for r in rows)
    n = len(lens)
    buckets = {label: 0 for _, label in _LEN_BUCKETS}
    for v in lens:
        for cap, label in _LEN_BUCKETS:
            if cap is None or v <= cap:
                buckets[label] += 1
                break

    def pct(x: int) -> float:
        return round(100.0 * x / n, 2)

    feats = {
        "bracket_action": sum(1 for r in rows if r[2]),
        "assistant_tell": sum(1 for r in rows if r[4]),
        "meow": sum(1 for r in rows if r[1]),
        "question_end": sum(1 for r in rows if r[3]),
        "hard_turn": sum(1 for r in rows if r[5]),
    }

    by_type: dict = {}
    for r in rows:
        t = r[6] or "?"
        b = by_type.setdefault(t, {"n": 0, "len_sum": 0})
        b["n"] += 1
        b["len_sum"] += int(r[0] or 0)
    for t, b in by_type.items():
        b["avg_len"] = round(b["len_sum"] / b["n"], 1) if b["n"] else 0
        del b["len_sum"]

    return {
        "window_days": days,
        "samples": n,
        "reply_len": {
            "avg": round(sum(lens) / n, 1),
            "median": lens[n // 2],
            "min": lens[0],
            "max": lens[-1],
            "buckets": buckets,
        },
        "feature_hits": feats,
        "feature_pct": {k: pct(v) for k, v in feats.items()},
        "by_type": by_type,
        "note": "只观测口径；不设目标数值（用户明说）",
    }




def compare_dialog_quality(split_ts: str, days: int = 7, min_side: int = 10) -> dict:
    """按时间点切分 reply_metrics，对比「修复前 / 修复后」两个窗口。

    2026-10-14 台账 T19 的教训：日级聚合会把「今天恰好短」误读成「我的改动有效」。
    要谈因果，必须**证明变化发生在修复之后**——所以本函数不猜时点，
    由调用方显式给 split_ts（通常是改动的部署/重启时刻），只做机械切分。

    只观测：不设阈值、不下结论、不判「变好了」。两个窗口各自给均值和分桶，
    差值留给读的人判断——判定需要的样本量取决于效应大小，代码不该替人拍板。

    但「不下结论」不等于「可以印一个假结论」（2026-10-14 T27 补）：
    - **单侧样本 < min_side 时不给差值**。实测触发过：bot 重启后只有 1 条新样本，
      工具照样印出「均值 -128.8」——那是 n=1 的噪声，与 T19 那个「降 61%」是同一种错误，
      只不过这次它出在**我自己的工具**里。**一个会被读成结论的数字，比没有数字更坏。**
    - 给差值时**同排印出标准误**（SE = 样本标准差 / √n）。这样小样本的差值一眼就能看出
      远小于噪声，不必等谁来提醒。
    """
    db = ROOT / 'memory.db'
    if not db.exists():
        return {'error': 'memory.db 不存在'}
    try:
        conn = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True, timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reply_metrics'")
        if not cur.fetchone():
            conn.close()
            return {'error': 'reply_metrics 表不存在（迁移未跑？）'}

        # ⚠ split_ts 是**调用方给的本地时刻**，而 ts 列本身就是 datetime('now','localtime') 存的本地时刻。
        # 所以**不能**对 split_ts 再套 datetime(?, 'localtime')——那是把本地时刻当 UTC 再转一次，
        # 2026-10-14 实测会变成 08:00（+8 小时偏移），把当天的行错误划到 before（T21 踩到）。
        # 下界（-N days）才需要走 datetime 运算，算完与 ts 同为字符串比较。
        def window(cond: str, params: tuple) -> dict:
            cur.execute(
                'SELECT reply_len, meow, bracket_action, question_end, assistant_tell '
                'FROM reply_metrics WHERE ts >= datetime(?, ?) AND ' + cond,
                (split_ts, f'-{int(days)} days') + params,
            )
            rows = cur.fetchall()
            if not rows:
                return {'samples': 0}
            lens = sorted(int(r[0] or 0) for r in rows)
            n = len(lens)
            buckets = {label: 0 for _, label in _LEN_BUCKETS}
            for v in lens:
                for cap, label in _LEN_BUCKETS:
                    if cap is None or v <= cap:
                        buckets[label] += 1
                        break
            mean = sum(lens) / n
            # 样本标准差（无偏，n-1）→ 标准误。n=1 时 SE 无意义，置 None。
            var = (sum((v - mean) ** 2 for v in lens) / (n - 1)) if n > 1 else 0.0
            se = round((var ** 0.5) / (n ** 0.5), 1) if n > 1 else None
            return {
                'samples': n,
                'avg': round(mean, 1),
                'se': se,
                'median': lens[n // 2],
                'buckets': buckets,
                'over_100_pct': round(100.0 * sum(1 for v in lens if v > 100) / n, 1),
                'meow_pct': round(100.0 * sum(1 for r in rows if r[1]) / n, 1),
                'hard_turn_pct': round(100.0 * sum(1 for r in rows if r[4]) / n, 1),
            }

        # 直接字符串比较：ts 与 split_ts 同为本地时刻文本，格式一致（YYYY-MM-DD HH:MM:SS）
        before = window("ts < ?", (split_ts,))
        after = window("ts >= ?", (split_ts,))
        conn.close()
    except Exception as e:
        return {'error': str(e)}

    out = {
        'split_ts': split_ts,
        'window_days': days,
        'before': before,
        'after': after,
        'note': '只观测口径；不设阈值、不判「变好」（用户明说）',
    }
    nb = before.get('samples') or 0
    na = after.get('samples') or 0
    if nb >= min_side and na >= min_side:
        out['delta'] = {
            'avg_len': round(after['avg'] - before['avg'], 1),
            'over_100_pct': round(after['over_100_pct'] - before['over_100_pct'], 1),
            'meow_pct': round(after['meow_pct'] - before['meow_pct'], 1),
        }
        # 噪声参考：两侧标准误的方和根。差值小于它就别当回事。
        se_b, se_a = before.get('se'), after.get('se')
        if se_b is not None and se_a is not None:
            out['delta_noise'] = round((se_b ** 2 + se_a ** 2) ** 0.5, 1)
            out['delta_note'] = (
                f'样本充足（before {nb} / after {na}，门槛 {min_side}）'
                f'；差值噪声约 ±{out["delta_noise"]}（两侧标准误合成）')
    elif nb and na:
        out['delta_skipped'] = (
            f'样本不足（before {nb} / after {na}，门槛 {min_side}）——'
            f'小样本差值会把噪声读成效果，与 T19 同形，故不给差值')
    else:
        out['delta_skipped'] = '某一侧样本为 0，不算差值——先积累数据'
    return out

def collect_tests() -> dict:
    tests = list((ROOT / "tests").glob("test_*.py"))
    lines = 0
    for t in tests:
        try:
            lines += len(t.read_bytes().split(b"\n"))
        except Exception:
            pass
    return {"test_files": len(tests), "test_lines": lines}


# ⚠ 函数名不要叫 snapshot：tests/test_integration_contract.py 的零调用闸门按**裸名**
# 收集调用点，而 CLASS_METHOD_BASELINE 里有一条 ('agent/knowledge_index.py', 'snapshot')。
# 叫这个名字会让闸门把那条基线判成"已接线"而假红（2026-10-14 实测，见台账 T2.3）。
def collect_all(full: bool) -> dict:
    t0 = time.time()
    snap = {
        "meta": {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "host": os.environ.get("COMPUTERNAME", "?"),
            "python": sys.version.split()[0],
            "full": full,
        },
        "fingerprints": collect_fingerprints(),
        "scale": collect_scale(),
        "tests": collect_tests(),
        "vulture": collect_vulture(),
        "radon": collect_radon(),
        "pyflakes": collect_pyflakes(),
        "pytest": collect_pytest(full),
        "db": collect_db(),
        "log_tail": collect_log_tail(),
        "dialog_quality": collect_dialog_quality(),
    }
    snap["meta"]["elapsed_s"] = round(time.time() - t0, 1)
    return snap


def compare(cur: dict, prev: dict) -> list[str]:
    lines = []
    a, b = prev, cur

    def num(d, *path, default=None):
        for k in path:
            if not isinstance(d, dict):
                return default
            d = d.get(k, default)
        return d

    checks = [
        ("pytest 通过数", num(b, "pytest", "passed"), num(a, "pytest", "passed"), "up"),
        ("pyflakes undefined", num(b, "pyflakes", "undefined"), num(a, "pyflakes", "undefined"), "down"),
        ("vulture 命中", num(b, "vulture", "total"), num(a, "vulture", "total"), "down"),
        ("radon CC>=25", num(b, "radon", "cc_ge_25"), num(a, "radon", "cc_ge_25"), "down"),
        ("agent 行数", num(b, "scale", "agent_lines"), num(a, "scale", "agent_lines"), "info"),
        ("测试文件数", num(b, "tests", "test_files"), num(a, "tests", "test_files"), "info"),
    ]
    for name, now, old, direction in checks:
        if now is None or old is None:
            lines.append(f"  {name}: 缺数据（{old} → {now}）")
            continue
        if now == old:
            lines.append(f"  {name}: {now}（不变）")
            continue
        worse = (direction == "up" and now < old) or (direction == "down" and now > old)
        mark = "  ⚠ 变差" if worse else "  ✅ 变好"
        if direction == "info":
            mark = "  ·"
        lines.append(f"{mark} {name}: {old} → {now}")

    # 行尾一致性（铁律 E3）
    for f, meta in (b.get("fingerprints") or {}).items():
        old_meta = (a.get("fingerprints") or {}).get(f)
        if old_meta and old_meta.get("lineending") != meta.get("lineending"):
            lines.append(f"  ⚠ 行尾被改写 {f}: {old_meta['lineending']} → {meta['lineending']}")
        elif old_meta and old_meta.get("sha256") != meta.get("sha256"):
            lines.append(f"  · 内容变更 {f}（{old_meta['sha256']} → {meta['sha256']}）")
    return lines




# ── 驱动力每日序列（2026-10-14 台账 T4.9，Stage 4 T4.1 的序列补采）──
# T4.1 的判定标准是「**每日**至少一条 < 0.85 且不总是 social/avoid」。
# 单点快照证明不了「每日」——需要时间序列。但序列不该占一个专门的 7 天 soak 周期，
# 而应**在日常运行中顺带积累**（用户要求：一次做一件事、不无休止消耗）。
# 所以这里只加一个 append：每次跑快照就往 CSV 追一行六维驱动力。
# **只观测**：不设阈值、不下结论（与 collect_dialog_quality 同口径）。
_DRIVES_SERIES = OUT_DIR / 'drives-series.csv'
_DRIVE_KEYS = ['social', 'commitment', 'curiosity_info', 'express',
               'curiosity_explore', 'avoid']


def _read_drives() -> dict:
    """读 .tangtang_self.json 的 drives.drives（只读）。缺失返回 {}。"""
    p = state_path('.tangtang_self.json')
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        return {'_error': str(e)}
    d = (raw.get('drives') or {}).get('drives') or {}
    return d if isinstance(d, dict) else {}


def append_drives_series() -> dict:
    """把当前六维驱动力追加成一行 CSV（表头只写一次）。返回本次写入的行。

    列固定为 ts + _DRIVE_KEYS，缺的维度写空——**列不许漂移**，
    否则序列无法跨时间比较（这正是本函数存在的意义）。
    """
    d = _read_drives()
    if not d or '_error' in d:
        return {'error': d.get('_error', 'drives 缺失')}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    new = not _DRIVES_SERIES.exists()
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    header = ','.join(['ts'] + _DRIVE_KEYS)
    row = ','.join([ts] + [str(d.get(k, '')) for k in _DRIVE_KEYS])
    with _DRIVES_SERIES.open('a', encoding='utf-8', newline='') as f:
        if new:
            f.write(header + chr(10))
        f.write(row + chr(10))
    return {'ts': ts, 'values': {k: d.get(k) for k in _DRIVE_KEYS},
            'file': str(_DRIVES_SERIES.relative_to(ROOT))}


def check_drives_series() -> list:
    """校验 CSV 列与表头一致（阳性对照用：改坏列必须被发现）。"""
    if not _DRIVES_SERIES.exists():
        return ['drives-series.csv 不存在']
    lines = _DRIVES_SERIES.read_text(encoding='utf-8').strip().split(chr(10))
    if not lines:
        return ['drives-series.csv 为空']
    expect = ['ts'] + _DRIVE_KEYS
    got = lines[0].split(',')
    problems = []
    if got != expect:
        problems.append(f'表头不符：{got} != {expect}')
    for i, ln in enumerate(lines[1:], 2):
        n = len(ln.split(','))
        if n != len(expect):
            problems.append(f'第 {i} 行列数 {n} != {len(expect)}')
    return problems

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="含全量 pytest（慢）")
    ap.add_argument("--compare", action="store_true", help="与上一份快照对比")
    ap.add_argument("--drives-series", action="store_true",
                    help="把当次六维驱动力追加到 drives-series.csv（T4.1 序列补采）")
    ap.add_argument("--check-series", action="store_true",
                    help="只校验 drives-series.csv 列完整性，不采集")
    ap.add_argument("--quality-split", metavar="TS", default="",
                    help="按 TS（如 2026-10-14 08:09）切分 reply_metrics，"
                         "对比修复前后两个窗口；只观测不判定")
    args = ap.parse_args()

    # ── T4.9：序列校验路径（阳性对照用，不写任何文件）──
    if args.check_series:
        problems = check_drives_series()
        if problems:
            for p in problems:
                print(f"  [序列问题] {p}")
            return 1
        print(f"  [序列正常] {_DRIVES_SERIES.relative_to(ROOT)}")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prev_files = sorted(OUT_DIR.glob("*.json"))
    snap = collect_all(args.full)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT_DIR / f"{stamp}.json"
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[快照] {path.relative_to(ROOT)}  ({snap['meta']['elapsed_s']}s)")
    print(f"  agent: {snap['scale']['agent_files']} 文件 / {snap['scale']['agent_lines']} 行 / "
          f"{snap['scale']['agent_defs']} def / {snap['scale']['agent_classes']} class")
    print(f"  测试: {snap['tests']['test_files']} 文件 / {snap['tests']['test_lines']} 行")
    print(f"  vulture: {snap['vulture']['total']}  |  radon CC>=25: {snap['radon'].get('cc_ge_25')}  |  "
          f"pyflakes undefined: {snap['pyflakes']['undefined']}")
    if not snap["pytest"].get("skipped"):
        print(f"  pytest: {snap['pytest'].get('passed')} passed / {snap['pytest'].get('failed')} failed")
    else:
        print("  pytest: 已跳过（--full 才跑）")

    if args.compare and prev_files:
        prev = json.loads(prev_files[-1].read_text(encoding="utf-8"))
        print(f"\n[对比] 上一份 {prev_files[-1].name}")
        for line in compare(snap, prev):
            print(line)

    # ── T21：按部署时刻切分，对比修复前后（只观测）──
    if args.quality_split:
        q = compare_dialog_quality(args.quality_split)
        if "error" in q:
            print(f"\n[质量切分] 失败：{q['error']}")
        else:
            b, a = q["before"], q["after"]
            print(f"\n[质量切分] split={q['split_ts']}  窗口={q['window_days']}天")
            print(f"  修复前: {b.get('samples', 0)} 条, 均值 {b.get('avg')}, "
                  f"超100字 {b.get('over_100_pct')}%, 喵 {b.get('meow_pct')}%")
            print(f"  修复后: {a.get('samples', 0)} 条, 均值 {a.get('avg')}, "
                  f"超100字 {a.get('over_100_pct')}%, 喵 {a.get('meow_pct')}%")
            if "delta" in q:
                d = q["delta"]
                print(f"  差值  : 均值 {d['avg_len']:+}, 超100字 {d['over_100_pct']:+}pp, "
                      f"喵 {d['meow_pct']:+}pp")
                nz = q.get("delta_noise")
                if nz is not None:
                    print(f"  噪声  : ±{nz}（两侧标准误合成）{q.get('delta_note', '')}")
            else:
                # 样本不足时不给差值——一个会被读成结论的数字，比没有数字更坏（T27）
                print(f"  (不给差值：{q.get('delta_skipped', '样本不足')})")

    # ── T4.9：顺带把驱动力序列追加一行（--drives-series）──
    if args.drives_series:
        r = append_drives_series()
        if "error" in r:
            print(f"\n[驱动力序列] 跳过：{r['error']}")
        else:
            vals = r["values"]
            shown = "  ".join(f"{k}={vals.get(k)}" for k in _DRIVE_KEYS)
            print(f"\n[驱动力序列] 已追加 → {r['file']}")
            print(f"  {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
