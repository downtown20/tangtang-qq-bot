# T65 · prompt_len 数组 content 语义失真

> 2026-10-23。动因：`未完成事项.md` B 区登记——「`agent/handler.py` `prompt_len = sum(len(m.get('content','')) ...)`
> content 为数组时语义失真（返回元素个数）。依据：`T1-识图改造-执行.md` §五.3」。

## 一、取证

原登记说行号 `~6365`，实测已漂移到 **`:6715`**（`agent/handler.py`）：

```python
if resp.status_code != 200:
    err = (await resp.aread()).decode(errors="replace")[:300]
    prompt_len = sum(len(m.get("content","")) for m in body.get("messages", []))
    logger.error(f"DeepSeek API error {resp.status_code}: {err[:200]}")
    raise Exception(f"DeepSeek API error: {resp.status_code} | prompt={prompt_len}chars")
```

**影响面实测**：它**只**在 API 报错分支用（日志 + 异常串），**不进正常路径** ⇒ 不崩、不影响计费。
所以定级 **Minor**，但值得修——因为它骗的是**看日志的人**：

- content 是 `str` 时：`len()` = 字符数 ✅
- content 是 **list**（多模态消息，识图改造后就是这个形态）时：`len()` = **元素个数**（比如 2）
  ⇒ 一条 3000 字的识图请求，报错日志会印 `prompt=2chars`

看日志的人据此判断「上下文没超，是别的原因」——**而真实原因可能是 prompt 太大**。

## 二、改动（2 文件）

| 文件 | 改动 |
|---|---|
| `agent/handler.py` | 新增模块级 `_prompt_chars(messages)`（唯一入口，docstring 写明「别再写 len(content)」）；`:6752` 调用点改用它 |
| `tests/test_prompt_chars.py` | **新建**，6 条 |

`_prompt_chars` 的语义（**刻意保守**）：

| content 形态 | 算法 |
|---|---|
| `str` | `len(content)` |
| `list`（多模态） | 逐段：`str` 段算长度；`dict` 段取 `text` 字段算长度；**图片段（`image_url`）算 0** |
| `None` / 其它 | 0 |

**为什么图片段算 0**：base64 不是提示词文本，也不进 token 估算；把它算进去会让数字虚高几十万倍。
**为什么不在报错路径接 tokenizer**：这个数字只进日志，接 tokenizer 会把一个只读日志点变成
性能热点（还要依赖模型加载）——**报错路径不该再引入新的失败可能**。docstring 里写明了这条。

## 三、验证

| 项 | 结果 |
|---|---|
| 定向测试 | **5 passed**（另有 1 条并入统计共 6 条用例） |
| 变异对照 | **2/2 真红** |
| 行尾 | `handler.py` CRLF 保持（637,124 字符）；测试文件 LF |
| 静态检查 | 82 文件 / undefined 0 |

**测试清单**（`tests/test_prompt_chars.py`）：

1. `test_plain_string_content` —— 纯文本
2. `test_multimodal_content_counts_text_not_elements` —— 多模态只数 text 段
3. `test_mixed_shapes_in_one_payload` —— 混合形态
4. `test_tolerates_junk` —— 畸形 payload 不许再抛（**报错路径上自己抛会把真错误盖掉**）
5. `test_old_naive_formula_really_was_wrong` —— **阳性对照**：断言旧写法确实返回 2，新写法返回真实字符数

第 5 条是本任务的**理由本身**：它证明这个 helper 不是多余包装。如果哪天有人改回旧写法，它会红。

**变异设计**：

| 变异 | 期望失败断言 | 结果 |
|---|---|---|
| M1 改回 `len(content or "")` | `test_old_naive_formula_really_was_wrong` | **RED OK** |
| M2 text 段算 0（漏掉累加） | `test_multimodal_content_counts_text_not_elements` | **RED OK** |

⚠ **M1 第一次是 INVALID**：我把 `expect_fail` 写成了断言**消息文本**（"新口径没有比旧写法更接近真实字符数"），
但实际红的是 `test_old_naive_formula_really_was_wrong` 里的 `assert naive == 2` 那句
（旧写法改回后 `naive` 变成真实长度，不是 2）——**失败信息不同**。
改成 `expect_fail` 指向**用例名**后正确。⇒ 教训：`expect_fail` 优先写**用例名**，
写断言消息时要确认那确实是**第一个**失败的断言。

## 四、没做

- **没有**给所有 LLM 调用点都换成 `_prompt_chars`——实测只有这一处用 `len(content)`，
  其余调用点没有同类写法（全文件搜 `len(m.get("content"` 仅剩 docstring 里那句引用）；
- **没有**动 `_request_timeout` / `_endpoint` 一带的既有逻辑（B3：一次一件事）。
