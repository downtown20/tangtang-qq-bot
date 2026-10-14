# GitNexus 使用指南

> 安装日期：2026-08-03
> 版本：1.6.6（npm 全局安装），Skill 来自 1.6.10-rc.148

## 什么是 GitNexus

一个代码知识图谱工具。把你的项目索引成节点（函数、类、文件）和边（调用、依赖、继承），然后 AI 可以通过 MCP 工具查询这些关系，做到精准重构、影响分析、代码审查。

**核心价值**：AI 不再靠 grep 猜代码关系——GitNexus 把依赖图、调用链提前算好了。

## 如何开启

### 安装（已完成，仅记录）

```bash
# 全局安装
npm install -g gitnexus@latest

# Node 24 需要跳过不兼容的可选语法（Dart/Swift/Proto/Kotlin 解析器）
# 先创建占位桩：
mkdir -p ~/AppData/Roaming/npm/node_modules/gitnexus/node_modules/tree-sitter-dart
echo '{"name":"tree-sitter-dart","version":"0.0.0","main":"./index.js"}' > ~/AppData/Roaming/npm/node_modules/gitnexus/node_modules/tree-sitter-dart/package.json
echo "module.exports = { default: {} };" > ~/AppData/Roaming/npm/node_modules/gitnexus/node_modules/tree-sitter-dart/index.js
# （tree-sitter-proto 和 tree-sitter-swift 同理）
```

### 索引项目

```bash
cd /d "<项目根>"
gitnexus analyze --skip-git   # 糖糖 2026-10-15 起已是 git 仓库；--skip-git 是既定用法，别去掉
```

### 注册 MCP（已完成）

```bash
gitnexus setup
```

## 如何使用

**不需要手动调用**。AI 会根据你的请求自动选择合适的 skill：

| 你说什么 | 自动触发 | 实际能力 |
|---------|---------|---------|
| "重命名这个函数"、"把这个模块拆出去"、"移动这个文件" | `gitnexus-refactoring` | 自动查找所有引用点，安全重命名/拆分/移动 |
| "这个改动会影响到什么"、"改了会不会崩"、"谁依赖这个" | `gitnexus-impact-analysis` | 上游/下游影响分析 |
| "帮我审查代码"、"Review 这个 PR"、"能合并吗" | `gitnexus-pr-review` | 代码审查、风险评估、测试覆盖检查 |
| "这段逻辑是怎么跑的"、"谁调用了这个函数"、"追踪调用链" | `gitnexus-exploring` | 执行流程追踪、架构理解 |
| "为什么这段代码报错"、"追踪这个 bug"、"这个错误从哪来的" | `gitnexus-debugging` | Bug 追踪、错误溯源 |
| "帮我做一份实现计划" | `gitnexus-plan` | 基于依赖图生成工程计划 |
| "执行这个计划" | `gitnexus-work` | 逐步实现，每步前自动做影响检查 |

也可以手动调用：输入 `/gitnexus-refactoring` 等。

## 前提条件

1. **必须先索引**：`gitnexus analyze --skip-git` 跑过一次
2. **索引不会自动更新**：大改代码后需重跑 `gitnexus analyze --skip-git`
3. **全局限定**：索引的是当前工作目录（`<项目根>`），不是整个用户目录

## 适用场景

- **重构前**：先让 AI 用 GitNexus 查出所有引用点，再动手
- **接手陌生代码**：让 AI 用 GitNexus 追踪调用链，不靠 grep 猜
- **改完验证**：让 AI 用 GitNexus 跑影响分析，确认没有遗漏

## 局限性

- 只能索引 Python/JS/TS/Go/Rust/Java 等常见语言，Dart/Swift 等因 Node 24 兼容性问题暂不支持
- 运行在 Windows 上，部分 tree-sitter 原生模块需要 Node 版本匹配
- 非 git 仓库缺少增量更新能力，每次需全量重索引
