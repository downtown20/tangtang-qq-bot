# T11 · 同步skills 失败无信号 —— 执行记录

> 完成日：2026-10-14 ｜ 状态：**已完成**

---

## 一、问题（四段式）

**位置**：`tools/同步skills.py` 的 `main()` 与 `__main__` 块

**证据**（`.trash/20261014-s5/t11_probe.py` + `t11_mutation.py`）：

```
robocopy 对 bogus dst 的返回码 = 16
  (<8 视为成功 -> False )   ← _robocopy() 判断是正确的
```

但 `main()` 只把结果用来**打印**，不据它设置退出码：

```python
# 修复前
def main():                      # ← 隐式返回 None
    for src, dst in MIRRORS:
        ok = _robocopy(src, dst)
        print(f"[{'OK' if ok else '失败'}] ...")   # ← 只打印

if __name__ == "__main__":
    try:
        main()                   # ← 返回值被丢掉
    finally:
        ...
```

**根因**：**判断正确但信号没传出去**。脚本**永远以退出码 0 结束**。

**后果**：自动化/CI 完全无法感知失败——只能靠人读 stdout 里的「失败」二字。
这与 `tools/准备发布.py` 注释里骂过的模式一模一样：「真正的红线只靠人读 stdout……
这个项目一天里两次把真 token 带进发布物，都是靠人眼漏过去的。」

---

## 二、修法（1 个文件）

```python
def main() -> bool:                    # ← 返回是否全部成功
    failed: list[str] = []
    for src, dst in MIRRORS:
        ...
        if not ok:
            failed.append(str(dst))
    ...
    if failed:
        print(f"\n[×] 镜像失败 {len(failed)} 个目标：{failed}")
        print("    上面标 [失败] 的那几条没同步过去——Codex 那边仍是旧技能。")
        return False
    return True

if __name__ == "__main__":
    _rc = 0
    try:
        if not main():
            _rc = 1
    finally:
        ...
        sys.exit(_rc)                  # ← 变成退出码
```

**没做的**：不动 `_robocopy()`（它本来就是对的）、不动 `_check_frontmatter`、不动 robocopy 参数（`/E /XD .git` 等）、不动 `isatty` 暂停逻辑。

---

## 三、验收判据逐条核对

| # | 判据 | 结果 |
|---|---|---|
 | 1 | 新增回归测试 | `tests/test_sync_skills_exit_code.py` **8 条** |
 | 2 | `pytest tests/ -q` 全绿 | **2643 passed, 0 failed**（2635 + 8，376s）|
 | 3 | `pyflakes` undefined = 0 | 81 文件，**0** |
 | 4 | 变异阳性对照 | **3/3**：M0 正常退出码 **0** / M1 dst 换成不可达盘符 → 退出码 **1** + stdout 含「镜像失败 1 个目标」/ M2 还原后 **0** |
 | 5 | 每次变异后字节还原 | 一致，sha=`b565bf9b83cd122a` |
 | 6 | 回滚锚点 | `tools/同步skills.py.bak-20261014-T11`（4404 B, sha `74cfaeb8fb580573`）|
 | 7 | 行尾保持 | 纯 LF |
 | 8 | AST 合法 | 通过 |

### 8 条测试覆盖

**结构闸门**：`main()` 声明 `-> bool`；源码里 `return True` 与 `return False` **都在场**（防退回隐式 None）；`__main__` 块里有 `sys.exit(_rc)` 与 `if not main():`。
**robocopy 判定**（这部分本来没坏，钉住防退化）：对不可达目标返回 `False`；对真实拷贝返回 `True` **且文件真的落地**。
**端到端**：正常跑退出码 0；把镜像目标改坏后退出码 **!= 0** 且 stdout 含「镜像失败」——**并在 finally 里字节还原**。

---

## 四、方法论记录

这条任务的修法极其简单（加返回值 + `sys.exit`），但**难度不在修，在发现**：
`_robocopy()` 返回 bool、`main()` 用了它打印「失败」——**读代码时一切看起来都对**。
只有把「脚本失败时退出码是多少」当成一个**要实测的量**，才看得见问题。

**⇒ 与 T10 同源：可靠性属性（退出码、命中数、区分度）必须实测，不能靠读代码推断。**

---

## 五、当前文件指纹

- `tools/同步skills.py` 5367 B sha `b565bf9b83cd122a` 纯 LF（修复前 4404 B / sha `74cfaeb8fb580573`）
- `tests/test_sync_skills_exit_code.py` 新建 8 条

探针：`.trash/20261014-s5/` 下 `t11_probe.py`、`t11_run.py`、`t11_mutation.py`。
