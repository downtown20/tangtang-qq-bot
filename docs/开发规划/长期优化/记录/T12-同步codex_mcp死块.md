# T12 · 同步codex_mcp 退化配置产出死块 —— 执行记录

> 完成日：2026-10-14 ｜ 状态：**已完成**

---

## 一、问题（四段式）

**位置**：`tools/同步codex_mcp.py` 的 `build_block()`（`main()` 为连带受害点）

**证据**（`.trash/20261014-s5/t12_probe.py`）：

```
case                   -> 输出
正常 url                 -> [mcp_servers.weird] url = "http://x/mcp"
退化：只有 args          -> [mcp_servers.weird] command = ""  args = ["a"]     <<< 死块
退化：空 dict            -> [mcp_servers.weird] command = ""  args = []        <<< 死块
退化：只有 env           -> [mcp_servers.weird] command = ""  args = []  env = {...}  <<< 死块
退化：command 空串        -> [mcp_servers.weird] command = ""  args = []        <<< 死块
退化：url 空串            -> [mcp_servers.weird] command = ""  args = []        <<< 死块
```

**根因**：`build_block` 只按 `cfg.get("url")` 真值分流——没有 url 就落进 `else`，
无条件写 `command = {cfg.get('command', '')}`，**从不检查 command 是否真的存在**。

**连带问题（同一个洞的另外两面）**：

1. `main()` 只按 `isinstance(cfg, dict)` 过滤 ⇒ 退化条目**照写不误**进 `~/.codex/config.toml`；
2. 退化条目**算进 `added` 计数**并打印「已写入 N 个服务器」——**虚假成功报告**。

**后果**：Codex 认出这个 server 却起不来，而同步输出说「已写入」。
**死配置比报错更贵**——报错会有人修，死配置不会。

**顺带发现**：`main()` 同样**没有退出码**（与 T11 同一课）。

---

## 二、修法（1 个文件，三处）

### 1. 新增可用性判据 `is_usable(cfg) -> bool`

```python
def is_usable(cfg) -> bool:
    """Codex 的 mcp_servers 条目必须二选一：有 url（HTTP/SSE），
    或有非空 command（stdio）。两者都没有就是死配置。"""
    if not isinstance(cfg, dict):
        return False
    if (cfg.get("url") or "").strip():
        return True
    return bool((cfg.get("command") or "").strip())
```

### 2. `build_block` 显式拒绝，不再静默产出

```python
def build_block(name: str, cfg: dict) -> str:
    if not is_usable(cfg):
        raise ValueError(
            f"mcp server {name!r} 既没有 url 也没有 command——"
            f"写进 config.toml 会是死配置，拒绝生成")
```

`else` 分支改用 `cfg['command']`（此时已保证非空）而非 `.get('command', '')`。

### 3. `main()` 过滤 + 点名 + 返回 bool

```python
added, skipped, unusable = [], [], []
for name in servers:
    if f"[mcp_servers.{name}]" in existing:
        skipped.append(name); continue
    if not is_usable(servers[name]):
        unusable.append(name); continue      # ← 不再算进 added
    added.append(name)
if unusable:
    print(f"⚠️ 跳过 {len(unusable)} 个不可用配置（既无 url 又无 command，写了也是死块）: ...")
...
return True / False
```

`__main__` 块加 `sys.exit(_rc)`（与 T11 同款）。

**没做的**：不动 TOML 序列化（`_toml_str`/`_inline_table`）、不动 `headers`/`env` 输出、不动 `isatty` 暂停逻辑。

---

## 三、验收判据逐条核对

| # | 判据 | 结果 |
|---|---|---|
 | 1 | 新增回归测试 | `tests/test_sync_codex_mcp_block.py` **28 条** |
 | 2 | `pytest tests/ -q` 全绿 | **2671 passed, 0 failed**（2643 + 28，402s）|
 | 3 | `pyflakes` undefined = 0 | 81 文件，**0** |
 | 4 | 变异阳性对照 | **4/4**：M1 `is_usable` 退回 isinstance → **13 红** / M2 摘 ValueError 护栏 → **27 红** / M0M3 绿 |
 | 5 | 每次变异后字节还原 | 两次还原均 sha=`5de8e6a47cf995f6` |
 | 6 | 回滚锚点 | `tools/同步codex_mcp.py.bak-20261014-T12`（3650 B, sha `9dfd8a20002940a3`）|
 | 7 | 行尾保持 | 纯 LF |
 | 8 | AST 合法 | 通过 |
 | 9 | 修复后行为实证 | `is_usable` 14 用例判定**全对**；`build_block` 对 4 类退化输入**全抛 ValueError**；可用输入仍正常渲染 |

### 28 条测试覆盖

**`is_usable`**（14 参数化）：url/command 正常 → True；空/`args` only/`env` only/空串/纯空格/None/字符串/列表 → False。
**`build_block` 拒绝**（6 参数化 + 1 结构闸门）：退化输入抛 `ValueError`；**结构闸门确保 `command = ""` 这个形状不可达**。
**happy path**（3 条）：url 块不出现 `command`；command 块不出现 `url`；`headers`/`env` 正常渲染。
**main 契约**（4 条）：`main` 声明 `-> bool`；源码里必须出现 `is_usable` 与 `unusable`（防退回旧过滤）；脚本必须有 `sys.exit(_rc)`；AST 合法。

---

## 四、S2 阶段三联收官

T10/T11/T12 是**同一个主要矛盾**的三个面——**「失败了但没人知道」**：

| 任务 | 面 | 修法 |
|---|---|---|
| T10 | 检查器口径漂移 ⇒ 检查报告是假的 | 两处正则逐字同形 + **修好失效的一致性测试** |
| T11 | 判断对了但退出码恒 0 ⇒ 自动化看不见 | `main() -> bool` + `sys.exit` |
| T12 | 产出死配置还报「已写入」⇒ 成功报告是假的 | `is_usable` 判据 + 拒绝生成 + 点名跳过 |

**三条都验证了同一个元教训：可靠性属性（命中数、退出码、区分度、可用性）必须实测，不能靠读代码推断。**
读 T11/T12 的代码时，「看起来都对」——因为逻辑确实写对了，只是**信号没有传到能被人/机看见的地方**。

---

## 五、当前文件指纹

- `tools/同步codex_mcp.py` 5801 B sha `5de8e6a47cf995f6` 纯 LF（修复前 3650 B / sha `9dfd8a20002940a3`）
- `tests/test_sync_codex_mcp_block.py` 新建 28 条

探针：`.trash/20261014-s5/` 下 `t12_probe.py`、`t12_verify2.py`、`t12_mutation.py`。
