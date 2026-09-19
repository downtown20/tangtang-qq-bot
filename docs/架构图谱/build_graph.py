#!/usr/bin/env python3
"""小糖糖 架构图谱生成器（真实数据，2026-09-05）

数据源：
  1. IMPORTS 边 —— 对自研核心 74 个文件做 AST 全量扫描（含函数内懒加载 import），
     绝对/相对路径解析到 agent/、onebot/ 内目标文件。
     （GitNexus 图谱的 Python IMPORTS 边严重缺失——agent/ 仅 87 条、handler.py 0 条，
      故不用；本 AST 提取 100% 覆盖、确定性。）
  2. CALLS 热度 —— GitNexus serve 导出的 full_graph.json 中两端可解析到自研范围的
     真实跨文件调用聚合（25110 条 CALLS 中的有效子集），叠加为边宽 + tooltip 明细。

产物：artifacts/图谱/糖糖架构图谱.html（浏览器打开，首次需联网加载 vis-network CDN）
"""
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
SRC = BASE / "artifacts" / "图谱" / "full_graph.json"
OUT = BASE / "artifacts" / "图谱" / "糖糖架构图谱.html"

ROOT_FILES = ["main.py", "onebot/ws_client.py", "糖糖控制台_qt.py"]
SCOPE_FILES = sorted(
    [str(p.relative_to(BASE)).replace("\\", "/") for p in (BASE / "agent").glob("*.py")]
    + ROOT_FILES)

LAYERS = [
    ("入口与协议", "#e05252", ROOT_FILES),
    ("路由·对话管理", "#e07b39", ["handler.py", "handler_commands.py", "handler_autonomy.py",
                                   "inbound_event.py", "message_batcher.py", "conversation_tracker.py",
                                   "interaction_contract.py", "catch_up.py", "tasks.py", "scheduler.py"]),
    ("决策装配·人格", "#d4a017", ["personality.py", "protocols.py", "context_builder.py",
                                   "scenario.py", "personalization.py", "group_style.py",
                                   "mood.py", "mood_tracker.py"]),
    ("记忆·状态·反思", "#3a9d5d", ["store.py", "memory.py", "memory_access.py", "knowledge.py",
                                    "knowledge_index.py", "embeddings.py", "reranker.py",
                                    "extraction_policy.py", "reflection.py", "self_state.py",
                                    "drives.py", "relationship.py", "perception.py"]),
    ("自治·主动行为", "#4a7bd4", ["proactive_decision.py", "interjection.py", "opinion.py",
                                  "album_patrol.py", "image_share.py", "daily_report.py",
                                  "send_actions.py", "platform_receipts.py", "action_plan.py",
                                  "action_executor.py", "action_contract.py"]),
    ("能力·LLM 工具", "#8e5bd4", ["skills.py", "calculator.py", "convert.py", "translate.py",
                                  "web_search.py", "games.py", "songs.py", "voice.py",
                                  "cosy_voice.py", "asr.py", "sticker.py", "image_gen.py",
                                  "vision_router.py", "vision_local.py", "file_reader.py",
                                  "diff_singer.py"]),
    ("基建·可观测", "#5b8a8a", ["async_io.py", "model_lock.py", "telemetry.py", "metrics.py",
                                "paths.py", "text_utils.py", "service_manager.py",
                                "self_check.py", "health_check.py"]),
]
LAYER_COLOR: dict[str, str] = {}
for _, color, files in LAYERS:
    for f in files:
        LAYER_COLOR["agent/" + f if f not in ROOT_FILES else f] = color
OTHER = "#9aa0a6"


SCOPE_SET = set(SCOPE_FILES)


def resolve_module(src_file: str, module: str | None, level: int) -> str | None:
    """把一个模块名解析回自研范围文件路径；范围外返回 None。

    - level==0 绝对：module='agent.telemetry' / 'onebot.ws_client' → 对应 .py
    - level==1 相对：module='async_io'（from .async_io import）→ src 所在包内同名 .py
    - level==1 + module=None（from . import protocols）：由调用方逐个 names 解析
    """
    if module is None:
        return None
    if level >= 2:
        return None                     # 本项目无 .. 级导入
    if level == 1:
        pkg = src_file.split("/")[0]    # agent / onebot（root 文件无同级模块）
        if pkg not in ("agent", "onebot"):
            return None
        chain = [pkg] + module.split(".")
    else:
        parts = module.split(".")
        if parts[0] not in ("agent", "onebot"):
            return None
        chain = parts                   # 自研包根开始
    p = Path(BASE) / "/".join(chain[:-1]) / f"{chain[-1]}.py"
    if not p.is_file():                 # 包级 import（如 onebot/__init__）不存在则放弃
        return None
    rel = str(p.relative_to(BASE)).replace("\\", "/")
    return rel if rel in SCOPE_SET else None


def collect_imports() -> dict[tuple[str, str], int]:
    """AST 扫描（含函数内懒加载 import），返回 (src,dst) → 导入条数。"""
    edge: dict[tuple[str, str], int] = defaultdict(int)
    for f in SCOPE_FILES:
        try:
            tree = ast.parse((BASE / f).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    dst = resolve_module(f, a.name, 0)
                    if dst and dst != f:
                        edge[(f, dst)] += 1
            elif isinstance(node, ast.ImportFrom):
                if node.module:         # from .async_io import x / from agent.y import z
                    dst = resolve_module(f, node.module, node.level)
                    if dst and dst != f:
                        edge[(f, dst)] += len(node.names)
                elif node.level >= 1:   # from . import protocols → 每个名字一个同级模块
                    for a in node.names:
                        dst = resolve_module(f, a.name, node.level)
                        if dst and dst != f:
                            edge[(f, dst)] += 1
    return edge


def collect_calls_from_graph() -> dict[tuple[str, str], int]:
    """GitNexus 全量图：两端可解析且均在自研范围的跨文件 CALLS 聚合。"""
    if not SRC.exists():
        return {}
    g = json.loads(SRC.read_text(encoding="utf-8"))
    fp_of = {n["id"]: (n.get("properties") or {}).get("filePath")
             for n in g["nodes"] if (n.get("properties") or {}).get("filePath")}
    scoped = set(SCOPE_FILES)
    out: dict[tuple[str, str], int] = defaultdict(int)
    for r in g["relationships"]:
        if r.get("type") != "CALLS":
            continue
        s, d = fp_of.get(r.get("sourceId", "")), fp_of.get(r.get("targetId", ""))
        if s in scoped and d in scoped and s != d:
            out[(s, d)] += 1
    return out


def main() -> None:
    print("AST 扫描模块依赖…")
    imports = collect_imports()
    print(f"import 边 {len(imports)} 条（语句数 {sum(imports.values())}）")

    print("叠加 GitNexus CALLS 热度…")
    calls = collect_calls_from_graph()
    print(f"CALLS 文件对 {len(calls)} 条（调用数 {sum(calls.values())}）")

    all_pairs = set(imports) | set(calls)
    edges = [{"src": s, "dst": d, "imports": imports.get((s, d), 0),
              "calls": calls.get((s, d), 0)} for s, d in sorted(all_pairs)]

    nodes = [{"file": f} for f in SCOPE_FILES]
    print(f"节点 {len(nodes)} · 关系 {len(edges)}")

    loc_map = {}
    for f in SCOPE_FILES:
        p = BASE / f
        try:
            loc_map[f] = sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
        except OSError:
            loc_map[f] = 0

    html = (HTML_TEMPLATE
            .replace("__NODES__", json.dumps(nodes, ensure_ascii=False))
            .replace("__EDGES__", json.dumps(edges, ensure_ascii=False))
            .replace("__LAYERS__", json.dumps(LAYER_COLOR, ensure_ascii=False))
            .replace("__LOC__", json.dumps(loc_map, ensure_ascii=False))
            .replace("__LEGEND__", "".join(
                f'<span class="lg"><i style="background:{color}"></i>{name}'
                f'<em>{len(files)}</em></span>' for name, color, files in LAYERS)))
    OUT.write_text(html, encoding="utf-8")
    print(f"✅ {OUT.name}（{OUT.stat().st_size / 1024:.0f} KB）")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>小糖糖 · 架构依赖图谱</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  :root { color-scheme: light; }
  html, body { margin:0; height:100%; font-family:"Segoe UI","Microsoft YaHei",sans-serif; background:#f7f8fa; }
  #top { position:fixed; top:0; left:0; right:0; z-index:9; display:flex; align-items:center; gap:12px;
         padding:8px 14px; background:rgba(255,255,255,.96); border-bottom:1px solid #e3e6ea; flex-wrap:wrap; }
  #top h1 { font-size:15px; margin:0 6px 0 0; font-weight:600; color:#1f2328; }
  #top h1 small { font-weight:400; color:#8b939e; margin-left:6px; }
  #legend { display:flex; gap:10px; flex-wrap:wrap; }
  .lg { font-size:12px; color:#4a5160; display:inline-flex; align-items:center; gap:5px; }
  .lg i { width:10px; height:10px; border-radius:3px; display:inline-block; }
  .lg em { font-style:normal; color:#b0b7c0; }
  #search { margin-left:auto; display:flex; gap:6px; align-items:center; }
  #search input { width:190px; padding:5px 9px; border:1px solid #d4d9e0; border-radius:6px; font-size:13px; }
  #stats { font-size:11px; color:#8b939e; padding-right:4px; white-space:nowrap; }
  #graph { position:fixed; inset:45px 0 0 0; }
  #tooltip { position:fixed; z-index:10; pointer-events:none; background:rgba(20,24,31,.92); color:#fff;
             padding:7px 10px; border-radius:6px; font-size:12px; max-width:420px; display:none; line-height:1.5; }
  #tooltip b { color:#ffd479; }
  .hint { position:fixed; bottom:10px; right:14px; z-index:9; font-size:11px; color:#a8aeb8;
          background:rgba(255,255,255,.8); padding:3px 8px; border-radius:10px; }
</style>
</head>
<body>
<div id="top">
  <h1>小糖糖 · 架构依赖图谱<small>自研核心 — import 结构(AST) + 调用热度(GitNexus)</small></h1>
  <div id="legend">__LEGEND__</div>
  <div id="search">
    <span id="stats"></span>
    <input id="q" placeholder="🔍 搜模块名（如 handler / memory / voice）" autocomplete="off">
  </div>
</div>
<div id="graph"></div>
<div class="hint">滚轮缩放 · 拖拽平移 · 悬停看明细（imports/calls 数）· 点选高亮</div>
<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<script>
const NODES = __NODES__;       // [{file}]
const EDGES = __EDGES__;       // [{src,dst,imports,calls}]
const LAYERS = __LAYERS__;     // {filePath: color}
const LOC = __LOC__;           // {filePath: lines}

const colorFor = f => LAYERS[f] || "#9aa0a6";
const nameOf = f => f.split("/").pop();

const deg = {};
for (const e of EDGES) { deg[e.src] = (deg[e.src]||0)+1; deg[e.dst] = (deg[e.dst]||0)+1; }

const groups = {};
for (const n of NODES) {
  const color = colorFor(n.file);
  groups[color] = groups[color] || [];
  const w = Math.sqrt(deg[n.file]||1);
  groups[color].push({
    id: n.file, label: nameOf(n.file), group: color,
    value: 6 + w*2.2,
    color: { background: color, border: "#fff", highlight: { background:"#ffd479", border:"#e0a800" } },
    font: { size: 11 + Math.min(4, w*0.7), color: "#2b313b", face: "Microsoft YaHei" },
    borderWidth: 1.2,
    title: `<b>${nameOf(n.file)}</b> · ${LOC[n.file]||"?"} 行<br><code>${n.file}</code>`
  });
}
const visNodes = Object.values(groups).flat();
const visEdges = EDGES.map(e => {
  const w = Math.min(6, 0.6 + 0.9*Math.log1p(e.imports + e.calls/4));
  return {
    from: e.src, to: e.dst, width: w,
    color: { color:"rgba(96,110,140,.30)", highlight:"#e05252", hover:"#4a7bd4" },
    arrows: { to: { enabled: true, scaleFactor: 0.45 } },
    title: `<b>${nameOf(e.src)}</b> → <b>${nameOf(e.dst)}</b><br>` +
           `imports ${e.imports} · calls ${e.calls}`,
    smooth: { enabled:true, type:"continuous", roundness:0.35 }
  };
});

const container = document.getElementById("graph");
const data = { nodes: new vis.DataSet(visNodes), edges: new vis.DataSet(visEdges) };
const opts = {
  autoResize: true,
  interaction: { hover:true, tooltipDelay:120, dragView:true, zoomView:true },
  physics: { solver:"forceAtlas2Based", stabilization:{ iterations:260 },
             forceAtlas2Based:{ gravitationalConstant:-42, centralGravity:0.008,
                                 springLength:110, springConstant:0.07, damping:0.4 } },
  nodes: { shape:"dot", scaling:{ min:6, max:22 } },
  edges: { selectionWidth:1.6 },
};
const net = new vis.Network(container, data, opts);
net.once("stabilizationIterationsDone", () => net.setOptions({ physics:false }));

const q = document.getElementById("q");
q.addEventListener("input", () => {
  const k = q.value.trim().toLowerCase();
  net.selectNodes(k ? visNodes.filter(n => n.file.toLowerCase().includes(k)).map(n => n.id) : []);
});
document.getElementById("stats").textContent =
  `${visNodes.length} 模块 · ${visEdges.length} 关系`;
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
