#!/usr/bin/env python3
"""小糖糖 记忆同步脚本 🧠

用法（在项目根目录执行）：
    python tools/同步记忆.py 检查            # 查看本机与快照状态（默认）
    python tools/同步记忆.py 打包            # 在「最后运行糖糖」的机器上执行——生成一致快照
    python tools/同步记忆.py 解包 [机器名]    # 在「准备运行糖糖」的机器上执行——应用快照（糖糖须停止）

为什么需要这个脚本：
    memory.db 是 WAL 模式的 SQLite——糖糖运行中最新数据在 memory.db-wal 文件里，
    主文件是不完整状态。Syncthing 直接同步 memory.db 会拿到缺 WAL 的旧数据，
    甚至损坏（本项目已产生过 10 个 memory.sync-conflict-*.db，包括 651MB 的大冲突）。

    本脚本用 SQLite 在线备份 (VACUUM INTO) 生成「一致快照」到 memory_sync/ 目录，
    该目录交给 Syncthing 同步；解包时用快照原子替换本地 memory.db。
    注意：.stignore 必须忽略 memory.db* 与运行时状态文件（见脚本末尾的配置）。

同步纪律：
    1. 台式机跑完糖糖 → python tools/同步记忆.py 打包
    2. 等 Syncthing 把 memory_sync/ 同步到笔记本
    3. 笔记本启动糖糖前 → python tools/同步记忆.py 解包（糖糖未运行）
    4. 反过来也一样——哪台最后跑，就从哪台打包
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 强制 UTF-8 输出：控制台自动打包时 stdout 被重定向（DEVNULL/管道），
# Python 会退回 GBK 编码——print 的 emoji（✅ 等）抛 UnicodeEncodeError
# 导致脚本退出码 1，快照却已生成 → 「显示失败但实际成功」。终端运行不受影响。
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "memory.db"
SYNC_DIR = BASE / "memory_sync"
SELF_STATE = BASE / ".tangtang_self.json"

STIGNORE_HINT = """
📌 项目根目录 .stignore 配置（Syncthing 用，需在两台机器都生效）：
    // 糖糖数据库——由 tools/同步记忆.py 管理，Syncthing 不碰
    memory.db
    memory.db-wal
    memory.db-shm
    // 运行时状态——每台机器各自生成，不同步
    .tangtang_self.json
    .mood_state.json
    .catch_up_state.json
    .scheduled_tasks.json
    .extraction_state.json
    // 已有冲突文件不再扩散
    *.sync-conflict-*
"""


def _machine() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname() or "unknown"


def _ts(mtime: float | None = None) -> str:
    return datetime.fromtimestamp(mtime or datetime.now().timestamp()).strftime("%Y-%m-%d %H:%M")


def sugar_running() -> bool:
    """检测本机是否在运行糖糖主进程 (main.py)"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*main.py*' }).Count"],
            capture_output=True, text=True, timeout=10,
        )
        return int(r.stdout.strip() or "0") > 0
    except Exception:
        pass
    # 兜底：wmic
    try:
        r = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
            capture_output=True, text=True, timeout=10,
        )
        return any("main.py" in ln and "python" in ln.lower() for ln in r.stdout.splitlines())
    except Exception:
        return False  # 无法检测——放行（解包前另有提示）


def _vacuum_into(src: Path, dst: Path) -> bool:
    """SQLite 在线备份：即使糖糖在运行也能得到一致快照。
    原子替换：先写临时文件，成功后才替换目标——失败时旧快照不受影响。"""
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        if tmp.exists():
            tmp.unlink()
        conn = sqlite3.connect(str(src), timeout=60)
        try:
            conn.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            conn.close()
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError("备份产物为空")
        tmp.replace(dst)  # 原子替换——旧快照保留到新快照写成功
        return True
    except Exception as e:
        print(f"  ❌ 数据库备份失败: {e}")
        print("     如果糖糖正在运行且锁表，请先停止糖糖后重试")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def _quick_check(db: Path) -> bool:
    try:
        conn = sqlite3.connect(str(db), timeout=30)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return bool(row and row[0] == "ok")
        finally:
            conn.close()
    except Exception:
        return False


def _db_modified(db: Path) -> float:
    """数据库实际修改时间——WAL 模式下糖糖的写入先进 -wal 文件，
    主文件 mtime 可能不更新。取主文件和 -wal 中较新的。"""
    t = db.stat().st_mtime
    wal = Path(str(db) + "-wal")
    if wal.exists():
        t = max(t, wal.stat().st_mtime)
    return t


def count_fork_diff(local_db: Path, snap_db: Path) -> int:
    """并发分叉检测：两库「同一 event_key（QQ 同一条入站消息）但内容不同」的记录数。
    双机同时在线收到同一条消息、各自写入不同内容 = 真并发分叉 → >0，解包会丢一方。

    2026-09-04 修复：旧实现按自增 id join——两库同源、各自新增量相同时 id 恰好对齐，
    把两库「各自新增的最后几条」误配成同一批（换机接力每次都报假分叉 2 条）。
    event_key 是 QQ seq 派生的业务稳定键（agent/store.py 建唯一索引，0 重复）：
    换机接力（快照 = 本机祖先）时新消息 seq 无重叠 → 恒 0。
    bot 自回复行 event_key 为空、天然不参与；2026-08-28 前的旧行无 event_key，
    只能靠 count_local_missing 方向守卫兜底，不参与本判定。
    检测失败返回 0（不阻塞解包）。"""
    try:
        conn = sqlite3.connect(str(local_db), timeout=30)
        safe = str(snap_db).replace("'", "''")
        conn.execute(f"ATTACH DATABASE '{safe}' AS snap")
        diff = conn.execute(
            "SELECT COUNT(*) FROM chat_log l JOIN snap.chat_log s ON l.event_key = s.event_key"
            " WHERE l.message != s.message AND l.event_key != '' AND l.is_bot_reply = 0"
        ).fetchone()[0]
        conn.execute("DETACH DATABASE snap")
        conn.close()
        return int(diff or 0)
    except Exception:
        return 0


def count_local_missing(local_db: Path, snap_db: Path, window: int = 300) -> int:
    """方向守卫：本机最新 window 条入站消息中「快照里没有」的条数。

    >0 = 本机存在快照未包含的新数据（本机最后时刻的对话没打包，或快照过旧），
    用该快照覆盖会静默丢失这些记录。正常接力（本机 = 快照的祖先）→ 本机记录
    全部在快照里 → 0。与 mtime 比较互补——WAL 模式下 mtime 不可靠、且两条旁支
    流的 mtime 无法表达「谁是谁的祖先」。"""
    try:
        conn = sqlite3.connect(str(local_db), timeout=30)
        safe = str(snap_db).replace("'", "''")
        conn.execute(f"ATTACH DATABASE '{safe}' AS snap")
        keys = [r[0] for r in conn.execute(
            "SELECT event_key FROM chat_log"
            " WHERE event_key != '' AND is_bot_reply = 0"
            " ORDER BY id DESC LIMIT ?", (window,))]
        if not keys:
            conn.execute("DETACH DATABASE snap")
            conn.close()
            return 0
        placeholders = ",".join("?" * len(keys))
        found = conn.execute(
            f"SELECT COUNT(*) FROM snap.chat_log WHERE event_key IN ({placeholders})",
            keys).fetchone()[0]
        conn.execute("DETACH DATABASE snap")
        conn.close()
        return len(keys) - int(found or 0)
    except Exception:
        return 0


# ─────────────────────────────────────────
# 命令实现
# ─────────────────────────────────────────

def cmd_check() -> int:
    print(f"  机器: {_machine()}")
    print(f"  糖糖进程: {'🟢 运行中' if sugar_running() else '⚪ 未运行'}")
    if DB.exists():
        st = DB.stat()
        print(f"  本机 memory.db: {st.st_size / 1e6:.1f} MB（{_ts(st.st_mtime)}）")
    else:
        print(f"  本机 memory.db: 不存在")

    print(f"\n  memory_sync/ 快照：")
    snaps = sorted(SYNC_DIR.glob("memory-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not snaps:
        print("    （无——先在运行糖糖的机器上执行「打包」）")
    for i, p in enumerate(snaps):
        mark = " ← 最新" if i == 0 else ""
        print(f"    {p.name}  {p.stat().st_size / 1e6:.1f} MB（{_ts(p.stat().st_mtime)}）{mark}")

    print(STIGNORE_HINT)
    return 0


def cmd_pack(if_newer: bool = False, force: bool = False) -> int:
    name = _machine()
    SYNC_DIR.mkdir(exist_ok=True)

    if DB.exists():
        snap = SYNC_DIR / f"memory-{name}.db"
        # --if-newer：数据没变化就跳过（自动打包场景，避免反复打包白费流量）
        # 注意用 _db_modified：WAL 模式下写入更新 -wal 文件，主文件 mtime 可能不变
        if if_newer and not force and snap.exists() and _db_modified(DB) <= snap.stat().st_mtime:
            print(f"  ⏭️ 数据未变化（memory.db 不新于现有快照），跳过打包")
            print(f"     如果确定有变化，请用: python tools/同步记忆.py 打包 --force")
            return 0
        if not _vacuum_into(DB, snap):
            return 1
        print(f"  ✅ 记忆快照: {snap.name}（{snap.stat().st_size / 1e6:.1f} MB，{_ts(snap.stat().st_mtime)}）")
    else:
        print("  ⚠️ 本机没有 memory.db，跳过")

    if SELF_STATE.exists():
        dst = SYNC_DIR / f"tangtang_self-{name}.json"
        shutil.copy2(SELF_STATE, dst)
        print(f"  ✅ 自我状态快照: {dst.name}")
    else:
        print("  ⚠️ 本机没有 .tangtang_self.json（首次运行糖糖后才会生成）")

    print("\n  ➡ 等 Syncthing 同步完成后，在另一台机器执行:")
    print("     python tools/同步记忆.py 解包")
    return 0


def cmd_unpack(machine: str | None) -> int:
    if sugar_running():
        print("  ❌ 糖糖正在运行——请先停止糖糖再解包（运行中覆盖数据库会损坏）")
        return 1

    snaps = sorted(SYNC_DIR.glob("memory-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not snaps:
        print("  ❌ memory_sync/ 下没有快照")
        print("     先在「最后运行糖糖的那台机器」执行: python tools/同步记忆.py 打包")
        print("     等 Syncthing 同步完成后，再回到本机执行解包")
        return 1

    chosen = None
    if machine:
        chosen = next((p for p in snaps if machine.lower() in p.name.lower()), None)
        if chosen is None:
            print(f"  ❌ 找不到 {machine} 的快照，可用的：{', '.join(p.name for p in snaps)}")
            return 1
    else:
        # 默认选「最新且非本机」的快照——本机快照是本机库的拷贝，解包它无意义
        my_name = (_machine().lower())
        others = [p for p in snaps if my_name not in p.name.lower()]
        if others:
            chosen = others[0]
            if len(others) < len(snaps):
                print(f"  ℹ 已排除本机快照（解包本机快照=覆盖自己，无意义），选: {chosen.name}")
        else:
            chosen = snaps[0]  # 只有本机快照——提示并继续（可能是误删库想恢复）
            print(f"  ⚠️ 只有本机快照可用（{chosen.name}）——解包它等于覆盖自己")

    # 覆盖方向检查：本机数据更新时提醒（防止旧数据覆盖新数据）
    # mtime 比较（WAL 下可能滞后）+ 精确方向守卫（本机最新记录是否都在快照里）双保险
    if DB.exists():
        local_missing = count_local_missing(DB, chosen)
        local_newer = DB.stat().st_mtime > chosen.stat().st_mtime
        if local_newer:
            print(f"  ⚠️ 本机 memory.db（{_ts(DB.stat().st_mtime)}）比快照（{_ts(chosen.stat().st_mtime)}）更新")
            print(f"     覆盖会用旧数据替换新数据。请确认本机糖糖已停止、且已执行过「打包」。")
            ans = input("     仍然继续覆盖? [y/N] ").strip().lower()
            if ans != "y":
                print("  已取消")
                return 1
        elif local_missing > 0:
            # 快照时间戳更新、但本机有快照没有的记录 = 本机是旁支流（最后时刻没打包）
            print(f"  ⚠️ 本机 memory.db 有 {local_missing} 条快照未包含的新记录")
            print(f"     （通常是本机最后时刻的对话没来得及打包）——覆盖会丢失它们。")
            print(f"     建议：先在本机执行「打包记忆」，等快照同步后再解包。")
            ans = input("     仍然继续覆盖? [y/N] ").strip().lower()
            if ans != "y":
                print("  已取消")
                return 1

        # 分叉检测：两库同一 chat_log id 内容不同的记录数 = 两台设备同时运行过的痕迹。
        # 正常换机（快照=本机旧版本）差异为 0；分叉时覆盖会静默丢失一方数据。
        diff = count_fork_diff(DB, chosen)
        if diff > 0:
            print(f"  ⚠️⚠️ 检测到记忆分叉：两库有 {diff} 条同一序号但内容不同的记录——")
            print(f"     说明两台设备同时运行过糖糖（各自写入同一序号），覆盖会丢失其中一方的数据。")
            print(f"     建议：先在 tools/_归档/记忆冲突存档 中比对，或手动导出差异后再决定。")
            ans = input("     仍要覆盖? [y/N] ").strip().lower()
            if ans != "y":
                print("  已取消")
                return 1

    # 本地备份
    if DB.exists():
        prev = DB.with_name("memory.db.previous")
        shutil.copy2(DB, prev)
        print(f"  💾 本地备份: {prev.name}")

    # 删掉旧的 WAL/SHM——防止残留日志污染新库
    for suf in ("-wal", "-shm"):
        p = Path(str(DB) + suf)
        if p.exists():
            p.unlink()

    shutil.copy2(chosen, DB)
    if not _quick_check(DB):
        print("  ❌ 快照完整性检查失败——请勿启动糖糖，检查 memory_sync/ 下的快照文件")
        return 1
    print(f"  ✅ 已应用快照 {chosen.name} → memory.db（完整性 OK）")

    # 自我状态（关系场/自我叙事）
    self_snaps = sorted(SYNC_DIR.glob("tangtang_self-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if self_snaps:
        shutil.copy2(self_snaps[0], SELF_STATE)
        print(f"  ✅ 已应用自我状态 {self_snaps[0].name} → .tangtang_self.json")
    else:
        print("  ⚠️ memory_sync/ 下没有自我状态快照（不影响启动，糖糖会重新积累）")

    print("\n  ✅ 解包完成——现在可以启动糖糖了")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    cmd = args[0] if args else "检查"
    machine = args[1] if len(args) > 1 else None
    if_newer = "--if-newer" in args
    force = "--force" in args

    if cmd in ("打包", "pack"):
        return cmd_pack(if_newer, force)
    if cmd in ("解包", "unpack"):
        return cmd_unpack(machine)
    if cmd in ("检查", "check", "-h", "--help"):
        return cmd_check()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
