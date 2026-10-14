"""torch 档位按本机设备自动落地（T54-A，2026-10-14）。

主人原话：「两台机器各装什么，写在 requirements.txt 里不代表已经自动化落地，
什么设备适合装也需要说明清楚。」

改之前的事实：`tools/安装糖糖.py` 在「记忆」和「语音」两组里各写死一份
`extra=[(["torch"], {"index-url": ".../whl/cpu"})]` —— **新装一台有独显的机器，
拿到的是一份没有 GPU 加速的糖糖**（100 字 198.2 秒，有卡时 5.4 秒），
而用户手册 §0.5 早就写着「安装程序会自己探测本机设备并选对应那一档」。

本文件钉四件事：
  1. 三条判据可判定（nvidia-smi 的算力 ≥ sm_70 / Intel Arc / 其余 → CPU），
     且判据①**不依赖 torch**（新机器 torch 还没装，devices.py 必然报 CPU）；
  2. 档位表与 agent/devices.py 的设备阶梯同源，不会各说各话；
  3. torch 必须单独一条 pip 命令（带 --no-deps，为保住 numpy<1.24）；
  4. 预览/干跑里必须看得见装的是哪一个 wheel（否则用户只看到「torch」三个字母）。
"""

import importlib.util
import runpy
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
INSTALLER = BASE / "tools" / "安装糖糖.py"
HEALTH = BASE / "tools" / "体检.py"


def _load(tmp_path: Path):
    """导入安装器，并把 BASE 指到临时目录（与 tests/test_installer_wiring.py 同款）。"""
    spec = importlib.util.spec_from_file_location("_installer_torch_under_test", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BASE = tmp_path
    # 落盘位置解析走真实项目配置，测试里换成临时目录——探测/计划逻辑与它无关。
    mod._deps_target = lambda rel: tmp_path / rel
    return mod


def _fake_machine(monkeypatch, mod, nvidia=None, intel=None, torch_dev=None):
    """把三条探测接缝换成固定值——测试要能模拟「另一台机器」。

    没有这个接缝，这套判据在 CI/本机上永远只跑真实硬件那一支
    （本机是 RTX 5070，torch 也装好了），等于没测。
    """
    monkeypatch.setattr(mod, "probe_nvidia",
                        lambda: nvidia or (False, "nvidia-smi 不在（未装 NVIDIA 驱动）", None))
    monkeypatch.setattr(mod, "probe_intel_arc",
                        lambda: intel or (False, "没有 Intel Arc / Iris 核显"))
    monkeypatch.setattr(mod, "torch_reported_device", lambda: torch_dev)


# ═══════════════════════════════════════════════════════════
# 一、判据：什么设备装什么
# ═══════════════════════════════════════════════════════════

def test_nvidia_gpu_selects_cuda_tier_before_torch_is_installed(tmp_path, monkeypatch):
    """有 NVIDIA 独显的**新机器**（torch 还没装）必须选 CUDA 档。

    这是本次修复的正面靶子：原实现只会装 CPU 版，装机的那一刻就注定了「有卡用不上」。
    """
    mod = _load(tmp_path)
    _fake_machine(monkeypatch, mod,
                  nvidia=(True, "NVIDIA GeForce RTX 5070", 120), torch_dev=None)
    info = mod.detect_torch_variant()
    assert info["variant"] == "cuda"
    assert info["pkgs"] == ["torch==2.13.0+cu130"]
    assert info["kwargs"]["index-url"] == "https://download.pytorch.org/whl/cu130"
    assert info["kwargs"]["no-deps"] is True, "cu 档必须带 --no-deps，否则 numpy 被顶到 2.x"
    assert any("nvidia-smi" in line for line in info["evidence"]),         f"判定依据要写明（用户要能复核）：{info['evidence']}"


def test_old_nvidia_card_below_sm70_falls_back_to_cpu(tmp_path, monkeypatch):
    """算力 < sm_70 的老卡：能列出来也没用——cu130 轮子里没有它的 kernel。"""
    mod = _load(tmp_path)
    _fake_machine(monkeypatch, mod, nvidia=(True, "Tesla K80", 37), torch_dev=None)
    info = mod.detect_torch_variant()
    assert info["variant"] == "cpu"
    assert any("sm_37" in line for line in info["evidence"])


def test_intel_arc_selects_xpu_tier_without_nvidia(tmp_path, monkeypatch):
    """笔记本那条线：Intel Arc 核显、没有 NVIDIA 卡 → xpu 档。"""
    mod = _load(tmp_path)
    _fake_machine(monkeypatch, mod, nvidia=(False, "nvidia-smi 不在", None),
                  intel=(True, "Intel(R) Arc(TM) 130T Graphics"), torch_dev=None)
    info = mod.detect_torch_variant()
    assert info["variant"] == "xpu"
    assert "torch==2.9.1+xpu" in info["pkgs"]
    assert info["kwargs"]["index-url"] == "https://download.pytorch.org/whl/xpu"


def test_no_accelerator_falls_back_to_cpu(tmp_path, monkeypatch):
    """既没 NVIDIA 也没 Intel Arc（含 AMD 卡）→ cpu 档，并把原因写出来。"""
    mod = _load(tmp_path)
    _fake_machine(monkeypatch, mod, nvidia=(False, "nvidia-smi 不在", None),
                  intel=(False, "AMD Radeon(TM) Graphics（xpu 档只支持 Arc / Iris Xe）"),
                  torch_dev=None)
    info = mod.detect_torch_variant()
    assert info["variant"] == "cpu"
    assert any("AMD" in line for line in info["evidence"]),         "落到 CPU 也必须说明为什么（AMD 不走 xpu）"


def test_manual_override_beats_detection(tmp_path, monkeypatch):
    """探测会判错（虚拟适配器 / 离线 / 老卡），所以要留一条明路。"""
    mod = _load(tmp_path)
    _fake_machine(monkeypatch, mod, nvidia=(True, "NVIDIA GeForce RTX 5070", 120),
                  torch_dev="cuda")
    assert mod.detect_torch_variant("cpu")["variant"] == "cpu"
    _fake_machine(monkeypatch, mod, torch_dev=None)
    assert mod.detect_torch_variant("cuda")["variant"] == "cuda"
    # 不认识的档位名不能把安装流程带进沟里：退回自动探测
    assert mod.detect_torch_variant("tpu")["variant"] == "cpu"


def test_variant_table_is_judgeable_not_a_slogan(tmp_path):
    """档位表必须与 agent/devices.py 的设备阶梯同源，且每档都能落成具体命令。"""
    mod = _load(tmp_path)
    ladder = tuple(mod.devices.DEVICE_LADDER)
    assert tuple(mod.TORCH_VARIANTS) == ladder,         f"档位表 {tuple(mod.TORCH_VARIANTS)} 与设备阶梯 {ladder} 各说各话"
    for dev, v in mod.TORCH_VARIANTS.items():
        assert v["pkgs"], f"{dev} 档没写装什么包"
        assert v["needs"] and v["cost"] and v["verdict"] and v["label"], \
            f"{dev} 档缺「什么设备适合装」或「代价」——那就成了口号"
        expected = "cu" if dev == "cuda" else dev
        assert f"/whl/{expected}" in v["kwargs"]["index-url"], \
            f"{dev} 档的 index-url 指错了源：{v['kwargs']['index-url']}"
    assert mod.TORCH_VARIANTS["cuda"]["kwargs"]["no-deps"] is True
    assert mod.TORCH_VARIANTS["xpu"]["kwargs"]["no-deps"] is True
    assert "no-deps" not in mod.TORCH_VARIANTS["cpu"]["kwargs"], \
        "cpu 档要正常拉依赖（它没有 numpy 冲突）"


# ═══════════════════════════════════════════════════════════
# 二、接线：档位真的进得了 pip 命令
# ═══════════════════════════════════════════════════════════

def test_torch_groups_no_longer_hardcode_a_cpu_wheel(tmp_path):
    """回归闸门：写死 CPU 版的那两行不许复活。"""
    text = INSTALLER.read_text(encoding="utf-8")
    assert '"extra": [(["torch"]' not in text, \
        "「记忆」/「语音」组又出现硬编码的 torch extra 清单了——档位会被它盖掉"
    mod = _load(tmp_path)
    cpu_sources = [d for d, v in mod.TORCH_VARIANTS.items()
                   if v["kwargs"]["index-url"].endswith("/whl/cpu")]
    assert cpu_sources == ["cpu"], \
        f"CPU 轮的地址只该挂在 cpu 档上，实际挂在：{cpu_sources}"
    for gname in ("记忆", "语音"):
        g = mod.GROUPS[gname]
        assert g.get("torch") is True, f"{gname} 组没声明需要 torch"
        for pkgs, kw in g.get("extra", []):
            assert not any(p.startswith("torch") for p in pkgs), \
                f"{gname} 组还留着硬编码的 torch extra（会盖掉档位选择）：{pkgs} {kw}"


def test_torch_is_its_own_pip_command_in_every_tier(tmp_path):
    """torch 不能与普通包混在一条 pip 命令里。

    cu / xpu 档带 --no-deps（为保住 numpy<1.24）；一旦和 jieba / transformers
    混在一条命令，--no-deps 会把**它们**的依赖也一起掐掉 —— 「装完不能用」。
    """
    mod = _load(tmp_path)
    for variant in ("cuda", "xpu", "cpu"):
        plan = mod._plan(["记忆", "语音"], variant)
        torch_items = [(p, k) for p, k in plan if any("torch" in x for x in p)]
        assert len(torch_items) == 1, \
            f"{variant}: torch 应只出 1 条命令（两组共用、去重后），实际 {len(torch_items)}"
        pkgs, kw = torch_items[0]
        assert all("torch" in x for x in pkgs), f"{variant}: torch 与普通包混装：{pkgs}"
        assert kw == mod.TORCH_VARIANTS[variant]["kwargs"]
        for other, other_kw in plan:
            if other == pkgs:
                continue
            assert other_kw == {}, f"{variant}: 普通包不该带 pip 参数：{other} {other_kw}"


def test_boolean_pip_flag_renders_as_bare_switch(tmp_path, monkeypatch):
    """--no-deps 是裸开关：拼成 "--no-deps True" pip 会直接报错。"""
    mod = _load(tmp_path)
    captured = {}

    class _R:
        returncode = 0

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    assert mod._install(["torch"], {"index-url": "https://x", "no-deps": True}) is True
    cmd = captured["cmd"]
    assert "True" not in cmd, f"布尔值被拼进命令行：{cmd}"
    assert cmd[-1] == "--no-deps", f"裸开关应该自成一项（不是 --no-deps True 这种拼法）：{cmd}"
    assert cmd[cmd.index("--index-url") + 1] == "https://x"


def test_dry_run_shows_which_wheel_would_be_installed(tmp_path, monkeypatch, capsys):
    """预览必须看得见装的是哪一个 wheel——只打「torch」等于没落地。"""
    mod = _load(tmp_path)
    _fake_machine(monkeypatch, mod,
                  nvidia=(True, "NVIDIA GeForce RTX 5070", 120), torch_dev=None)
    assert mod.install_groups(["记忆"], dry_run=True) is True
    out = capsys.readouterr().out
    assert "torch 档位：CUDA" in out
    assert "torch==2.13.0+cu130" in out
    assert "--index-url https://download.pytorch.org/whl/cu130" in out
    assert "--no-deps" in out
    assert "[本机]" in out, "档位表里应标出本机落哪一档"


def test_cuda_torchaudio_is_soft_so_a_deleted_dev_wheel_cannot_break_install(
        tmp_path, monkeypatch):
    """cu 档的 torchaudio 是 dev 轮子（cp310-win 没有 2.13 稳定轮），只能算软增补。"""
    mod = _load(tmp_path)
    soft = mod.torch_soft_spec("cuda")
    assert soft is not None
    assert soft[0] == ["torchaudio==2.11.0.dev20260727+cu130"]
    assert mod.torch_soft_spec("cpu") is None, "cpu 档的 torchaudio 是稳定轮，走硬安装"
    assert "torchaudio==2.9.1+xpu" in mod.TORCH_VARIANTS["xpu"]["pkgs"], \
        "xpu 档有匹配的稳定 torchaudio，不该降级成软增补"

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    def _fake_run(cmd, **kw):
        return _R(1 if any("torchaudio" in c and "dev" in c for c in cmd) else 0)

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    assert mod.install_groups(["记忆"], dry_run=False, variant="cuda") is True, \
        "dev 轮子 404 不该把整个安装判成失败（torch 本体已可用）"


# ═══════════════════════════════════════════════════════════
# 三、体检与文档：说得出「我装的是哪一档」
# ═══════════════════════════════════════════════════════════

def test_health_reports_which_wheel_is_actually_installed(capsys):
    """用户手册 §0.5 承诺体检第 6 节能看出自己是哪一档——这里钉住它。"""
    import torch
    ns = runpy.run_path(str(HEALTH))
    ns["check_gpu"]()
    out = capsys.readouterr().out
    assert "torch 档位：" in out, f"体检没报实际装的档位：{out}"
    ver = torch.__version__
    low = ver.lower()
    if "+cu" in low:
        assert "torch 档位：CUDA" in out
    elif "+xpu" in low:
        assert "torch 档位：XPU" in out
    elif "+cpu" in low:
        assert "torch 档位：CPU" in out


def test_requirements_documents_the_same_rules_and_no_dead_pointer():
    """requirements.txt 里「装什么」必须与本文件的判据同源，且不指向已删的脚本。"""
    text = (BASE / "requirements.txt").read_text(encoding="utf-8")
    assert "tools/安装依赖.py" not in text, "头部还写着已不存在的 tools/安装依赖.py"
    assert "tools/安装糖糖.py" in text
    assert "--torch-variant" in text, "requirements 要写明安装器已自动化、可人工覆盖"
    assert "nvidia-smi" in text and "sm_70" in text, "三条判据要写进清单文件"
    assert "Intel Arc" in text and "ROCm" in text, "AMD / Intel 的分档理由要写明"
