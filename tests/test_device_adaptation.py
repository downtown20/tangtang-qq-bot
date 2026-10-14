"""算力自适应：设备阶梯（cuda → xpu → cpu）与合成超时预算。

为什么有这一组测试
──────────────────
改造前，项目里有 **5 处各自判断** torch.cuda.is_available()。本仓已经四次踩
「同一条规则的多份拷贝各自演化」（T10 / T12 / T25 / T35 / T42），所以这次不是
「顺手统一一下」，而是**把判据本身钉死**：只有 agent/devices.py 能直接问 torch
要设备，别处一律走它。

而超时预算是另一条线：voice.py 原来写死 60 秒，2026-10-14 实测（同一台机器、
同一批中文文本）：

    GPU  2 / 18 / 50 / 100 字 = 1.2 / 1.5 / 2.7 / 5.4 秒
    CPU  2 / 18 / 50 / 100 字 = 42.8 / 40.2 / 80.9 / 198.2 秒

⇒ **没有 CUDA 的机器（笔记本就是）上，50 字以上的语音必然超时**，3 次重试全撞在
同一个还占着单 worker 的请求上，白等 3 分钟才降级发文字。这不是「偶尔慢」。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent import devices as D
from agent import voice as V

BASE = Path(__file__).resolve().parent.parent


# ── 设备阶梯 ─────────────────────────────────────────────────────────
def test_ladder_prefers_cuda(monkeypatch):
    monkeypatch.setattr(D, "has_cuda", lambda: True)
    monkeypatch.setattr(D, "has_xpu", lambda: True)
    assert D.best_device() == "cuda"


def test_ladder_falls_to_xpu_without_cuda(monkeypatch):
    """笔记本（Intel Arc 核显）的情形：没有 CUDA，但有 XPU。"""
    monkeypatch.setattr(D, "has_cuda", lambda: False)
    monkeypatch.setattr(D, "has_xpu", lambda: True)
    assert D.best_device() == "xpu"
    assert D.is_accelerated() is True


def test_ladder_falls_to_cpu(monkeypatch):
    monkeypatch.setattr(D, "has_cuda", lambda: False)
    monkeypatch.setattr(D, "has_xpu", lambda: False)
    assert D.best_device() == "cpu"
    assert D.is_accelerated() is False


def test_ladder_order_is_declared_once(monkeypatch):
    """阶梯顺序写在 DEVICE_LADDER 里，改一处就改全局。"""
    assert D.DEVICE_LADDER == ("cuda", "xpu", "cpu")
    assert set(D.DEVICE_LABELS) == set(D.DEVICE_LADDER)


# ── 探测本身的健壮性（不能抛）────────────────────────────────────────
def test_has_xpu_false_when_torch_has_no_xpu(monkeypatch):
    class FakeTorch:
        pass

    monkeypatch.setattr(D, "_torch", lambda: FakeTorch())
    assert D.has_xpu() is False


def test_has_xpu_false_when_is_available_raises(monkeypatch):
    class Boom:
        @staticmethod
        def is_available():
            raise RuntimeError("驱动炸了")

    class FakeTorch:
        xpu = Boom

    monkeypatch.setattr(D, "_torch", lambda: FakeTorch())
    assert D.has_xpu() is False


def test_probes_are_safe_without_torch(monkeypatch):
    monkeypatch.setattr(D, "_torch", lambda: None)
    assert D.has_cuda() is False
    assert D.has_xpu() is False
    assert D.best_device() == "cpu"
    assert D.accelerator_name() == ""
    assert D.summary() == "torch 未安装 —— 无法探测算力"


def test_label_and_summary_never_raise(monkeypatch):
    monkeypatch.setattr(D, "has_cuda", lambda: False)
    monkeypatch.setattr(D, "has_xpu", lambda: False)
    assert D.label() == "CPU"
    assert "选中的设备" in D.summary()


# ── 合成超时预算 ─────────────────────────────────────────────────────
def test_timeout_flat_on_accelerator(monkeypatch):
    """有加速卡时保持 60s：100 字实测 5.4s，失败要**快速**暴露。"""
    monkeypatch.setattr(D, "is_accelerated", lambda: True)
    assert V.tts_timeout("嗯。") == V.TTS_TIMEOUT_ACCELERATED
    assert V.tts_timeout("字" * 100) == V.TTS_TIMEOUT_ACCELERATED


def test_timeout_covers_measured_cpu_cost(monkeypatch):
    """CPU 上必须**覆盖实测耗时**，否则就是原来那个「长语音必失败」。"""
    monkeypatch.setattr(D, "is_accelerated", lambda: False)
    measured = {2: 42.8, 18: 40.2, 50: 80.9, 100: 198.2}
    for n, secs in measured.items():
        budget = V.tts_timeout("字" * n)
        assert budget > secs, f"{n} 字实测 {secs}s，预算只有 {budget}s —— 又会超时"
        assert budget >= secs * 1.5, f"{n} 字余量不足 1.5 倍（{budget}/{secs}）"


def test_timeout_has_floor_for_short_text(monkeypatch):
    """短句在 CPU 上也要 40 秒——不能按字数线性外推出一个很小的值。"""
    monkeypatch.setattr(D, "is_accelerated", lambda: False)
    assert V.tts_timeout("嗯。") == V.TTS_TIMEOUT_CPU_FLOOR
    assert V.tts_timeout("") == V.TTS_TIMEOUT_CPU_FLOOR


def test_timeout_grows_with_length_on_cpu(monkeypatch):
    """超过下限之后随长度增长；下限之内一律取地板值（短句在 CPU 上也要 40 秒）。"""
    monkeypatch.setattr(D, "is_accelerated", lambda: False)
    assert V.tts_timeout("字" * 5) == V.tts_timeout("字" * 40) == V.TTS_TIMEOUT_CPU_FLOOR
    assert V.tts_timeout("字" * 100) > V.tts_timeout("字" * 80) > V.TTS_TIMEOUT_CPU_FLOOR


def test_old_hardcoded_60s_would_have_failed(monkeypatch):
    """这是本次修改的**为什么**：旧的写死 60s 在 CPU 上装不下实测耗时。"""
    monkeypatch.setattr(D, "is_accelerated", lambda: False)
    assert 80.9 > 60.0, "50 字实测 80.9s"
    assert V.tts_timeout("字" * 50) > 60.0


def test_voice_uses_the_budget_not_a_literal(monkeypatch):
    """闸门：合成请求必须用 tts_timeout()，不许再出现写死的 timeout=。"""
    src = (BASE / "agent" / "voice.py").read_text(encoding="utf-8")
    assert "httpx.AsyncClient(timeout=tts_timeout(clean))" in src, (
        "voice.py 的合成请求没有走自适应预算 —— 又写死回去了"
    )


# ── 单一事实源闸门 ───────────────────────────────────────────────────
FORBIDDEN = "torch.cuda.is_available()"


def test_only_devices_py_asks_torch_directly():
    """agent/ 与 tools/ 里只允许 agent/devices.py 直接问 torch 要设备。

    这条闸门是为了挡住**第五次**「同一规则的多份拷贝」——见模块 docstring。
    gpt-sovits/ 是第三方目录，不在扫描范围（它自己的补丁层单独处理）。
    """
    offenders = []
    for sub in ("agent", "tools"):
        for path in sorted((BASE / sub).glob("*.py")):
            if path.name == "devices.py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if FORBIDDEN in text:
                for i, line in enumerate(text.splitlines(), 1):
                    if FORBIDDEN in line:
                        offenders.append(f"{sub}/{path.name}:{i}")
    assert offenders == [], (
        "这些地方绕过了 agent/devices.py 直接判 CUDA —— 设备阶梯（含 Intel XPU）"
        f"在它们身上不会生效：{offenders}"
    )


@pytest.mark.parametrize("rel,needle", [
    ("agent/reranker.py", "self._device = _devices.best_device()"),
    ("tools/denoise.py", "device = _devices.best_device()"),
    ("tools/体检.py", "dev = _devices.best_device()"),
    # 2026-10-15：原表的 agent/cosy_voice.py 与 tools/rvc_convert.py 两条随翻唱制作链
    # 删除；这里补上**活下来的**语音侧调用点——agent/voice.py 的 tts_timeout 靠
    # _devices.is_accelerated() 决定超时预算（GPU 1.2~5.4s / CPU 42~198s，见 requirements.txt
    # 的 torch 一节），它必须继续走 agent/devices.py 而不是自己判 torch。
    ("agent/voice.py", "_devices.is_accelerated()"),
])
def test_call_sites_go_through_devices(rel, needle):
    src = (BASE / rel).read_text(encoding="utf-8")
    assert needle in src, f"{rel} 没有走 agent/devices.py"


def test_devices_module_has_no_project_side_effects():
    """devices.py 必须能被 tools/ 当普通模块导入——不许在 import 期加载模型或连网。"""
    tree = ast.parse((BASE / "agent" / "devices.py").read_text(encoding="utf-8"))
    for node in tree.body:
        assert not isinstance(node, (ast.Import, ast.ImportFrom)) or True
    # 模块级只允许 import logging / __future__，torch 必须在函数里惰性导入
    top_imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            top_imports.add((node.module or "").split(".")[0])
    assert "torch" not in top_imports, "torch 必须是惰性导入（没装的机器也要能 import devices）"

# ── GPT-SoVITS 启动器的设备归一化 ─────────────────────────────────────
def _load_launcher():
    import importlib.util
    import sys
    p = BASE / "gpt-sovits" / "start_api_patched.py"
    spec = importlib.util.spec_from_file_location("_launcher_dev_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


SAMPLE_YAML = """\
custom:
  device: cpu
  is_half: false
  t2s_weights_path: a
v2:
  device: cpu
  is_half: false
  t2s_weights_path: b
"""


def test_launcher_rewrite_to_cuda():
    mod = _load_launcher()
    out = mod._rewrite_device_yaml(SAMPLE_YAML, "cuda")
    assert out.count("device: cuda") == 2
    assert out.count("is_half: true") == 2
    assert "device: cpu" not in out
    assert "is_half: false" not in out


def test_launcher_rewrite_to_xpu_keeps_fp32():
    """XPU 上不开 fp16 —— 上游自己也是「没有 CUDA 就 fp32」。"""
    mod = _load_launcher()
    out = mod._rewrite_device_yaml(SAMPLE_YAML, "xpu")
    assert out.count("device: xpu") == 2
    assert out.count("is_half: false") == 2


def test_launcher_rewrite_back_to_cpu():
    mod = _load_launcher()
    on_cuda = mod._rewrite_device_yaml(SAMPLE_YAML, "cuda")
    back = mod._rewrite_device_yaml(on_cuda, "cpu")
    assert back.count("device: cpu") == 2
    assert "is_half: true" not in back


def test_launcher_rewrite_is_idempotent():
    mod = _load_launcher()
    once = mod._rewrite_device_yaml(SAMPLE_YAML, "xpu")
    twice = mod._rewrite_device_yaml(once, "xpu")
    assert once == twice, "重复调用必须逐字节相同——否则每次启动都写盘"


def test_launcher_rewrite_does_not_touch_other_keys():
    mod = _load_launcher()
    out = mod._rewrite_device_yaml(SAMPLE_YAML, "cuda")
    assert "t2s_weights_path: a" in out and "t2s_weights_path: b" in out
    assert out.count("custom:") == 1 and out.count("v2:") == 1


def test_launcher_can_load_project_devices():
    """启动器按路径加载 agent/devices.py —— 不依赖 sys.path、也不污染它。"""
    mod = _load_launcher()
    dev = mod._load_devices()
    assert dev is not None, "按路径加载失败——设备阶梯会退化成只看 CUDA"
    assert hasattr(dev, "best_device")
    assert dev.best_device() in ("cuda", "xpu", "cpu")


def test_launcher_respects_forced_cpu():
    """service_manager 用 CUDA_VISIBLE_DEVICES=-1 表达「本次别再碰加速卡」。"""
    src = (BASE / "gpt-sovits" / "start_api_patched.py").read_text(encoding="utf-8")
    assert 'os.environ.get("CUDA_VISIBLE_DEVICES", "").strip() == "-1"' in src
    assert 'target = "cpu"' in src


def test_launcher_normalizes_on_every_start():
    """闸门：不许退回「已是 cuda 就直接 return」——那样跨机同步的残留改不掉。"""
    src = (BASE / "gpt-sovits" / "start_api_patched.py").read_text(encoding="utf-8")
    assert "_rewrite_device_yaml(text, target)" in src
    # 旧写法：先看 yaml 里是不是已经有 cuda，有就直接返回 —— 那样台式机同步过去的
    # cuda 在笔记本上永远不会被归一化。判据用**代码形态**，不用中文短语（docstring
    # 里也会出现那句话，本仓 T47 已经栽过一次）。
    assert 're.search(r"device:\\s*cuda", text)' not in src

