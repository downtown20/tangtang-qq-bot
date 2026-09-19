"""安装器的「配置接线」契约（2026-09-18）。

发布形态从「三个版本包」改为「一个包 + 安装器选功能」后，**接线成了正确性的核心**：
用户勾了语音、6G 模型下完，如果配置开关没跟着拨到 on，糖糖启动后一声不吭——
装完不能用，等于白装。

历史上安装器只会把功能**关掉**（`adjust_config` 唯一逻辑是"没选语音 → 关语音"），
从不会打开。本文件钉住双向接线，防止回归成单向。

⚠ 这些测试会真的 import 安装器模块（它会加载 tools/同步记忆.py，无重副作用），
   然后把模块级 BASE 指向 tmp_path，全程只碰临时目录。
"""

import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
INSTALLER = BASE / "tools" / "安装糖糖.py"

# 与安装器 FEATURES 下标对齐
FEAT_MEMORY, FEAT_VOICE, FEAT_VISION, FEAT_SING, FEAT_CONSOLE = 0, 1, 2, 3, 5


def _load(tmp_path: Path):
    """导入安装器并把它的 BASE 指到临时项目根"""
    spec = importlib.util.spec_from_file_location("_installer_under_test", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BASE = tmp_path
    return mod


def _seed_example(tmp_path: Path, mod, **overrides) -> None:
    """写一份 config.example.yaml——模拟发布包里那份（开关默认关，来自生产配置）"""
    data = {
        "bot": {"qq_id": "1", "owner_qq": "2"},
        "llm": {"vision": {"enabled": False, "provider": "local"}},
        "voice": {"enabled": False, "provider": "gpt-sovits"},
    }
    for path, value in overrides.items():
        node = data
        keys = path.split("__")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    import yaml
    (tmp_path / "config.example.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _read_cfg(tmp_path: Path) -> dict:
    import yaml
    return yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))


def test_selected_features_are_wired_on(tmp_path):
    """勾了的功能必须被打开——这是「装了就能用」的实现"""
    mod = _load(tmp_path)
    _seed_example(tmp_path, mod)
    fresh = mod.bootstrap_config(dry_run=True)   # dry-run 不落盘，但语义上是"新建"
    assert fresh is True
    # 真建一次
    mod.bootstrap_config(dry_run=False)
    mod.wire_config([FEAT_MEMORY, FEAT_VOICE, FEAT_VISION, FEAT_CONSOLE],
                    fresh=True, dry_run=False)
    cfg = _read_cfg(tmp_path)
    assert cfg["voice"]["enabled"] is True, "勾了语音却没打开 voice.enabled —— 糖糖不会说话"
    assert cfg["voice"]["asr_enabled"] is True, "勾了语音却没打开 asr_enabled —— 听不懂语音消息"
    assert cfg["voice"]["provider"] == "gpt-sovits", "勾了语音应走本地引擎"
    assert cfg["llm"]["vision"]["enabled"] is True, "勾了识图却没打开 vision.enabled"


def test_unselected_features_are_wired_off(tmp_path):
    """没勾的功能要干净地关掉——避免控制台显示可用、点了没反应"""
    mod = _load(tmp_path)
    _seed_example(tmp_path, mod, voice__enabled=True, llm__vision__enabled=True)
    mod.bootstrap_config(dry_run=False)
    mod.wire_config([FEAT_MEMORY, FEAT_SING, FEAT_CONSOLE], fresh=True, dry_run=False)
    cfg = _read_cfg(tmp_path)
    assert cfg["voice"]["enabled"] is False
    assert cfg["voice"]["asr_enabled"] is False
    assert cfg["llm"]["vision"]["enabled"] is False


def test_voice_provider_follows_feature(tmp_path):
    """没装引擎就必须切走 provider——否则每次启动都去拉一个不存在的本地 TTS 服务。

    agent/handler.py 的 _is_full_mode 只看 voice.provider，不看 voice.enabled。
    """
    mod = _load(tmp_path)
    _seed_example(tmp_path, mod)
    mod.bootstrap_config(dry_run=False)
    mod.wire_config([FEAT_MEMORY, FEAT_CONSOLE], fresh=True, dry_run=False)   # 不勾语音
    cfg = _read_cfg(tmp_path)
    assert cfg["voice"]["provider"] == "edge-tts", \
        "没勾语音却留着 gpt-sovits —— 启动时会去拉不存在的本地 TTS 服务"


def test_existing_config_is_not_silently_rewritten(tmp_path, monkeypatch):
    """已有配置不能静默改——补装依赖时不该动用户现有设置"""
    mod = _load(tmp_path)
    _seed_example(tmp_path, mod)
    mod.bootstrap_config(dry_run=False)
    mod.wire_config([FEAT_MEMORY, FEAT_CONSOLE], fresh=True, dry_run=False)   # 先建好
    before = _read_cfg(tmp_path)

    # 第二次以「非新建」身份跑，用户对确认回答 n
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    mod.wire_config([FEAT_MEMORY, FEAT_VOICE, FEAT_CONSOLE], fresh=False, dry_run=False)
    assert _read_cfg(tmp_path) == before, "用户拒绝了确认，配置不该被改动"


def test_asr_model_url_matches_runtime_code():
    """安装器预取的 ASR 模型，必须与 agent/asr.py 懒加载时下的那个是同一个。

    两边分开写是因为 tools/ 不该 import agent/（安装器要在依赖装好之前就能跑）。
    一旦上游换了模型版本而只改了一处，就会出现「安装器下了一份、运行时又下一份」——
    233M 白下，还多等一次。用文本解析钉住，不引入 import 依赖。
    """
    import re
    asr_src = (BASE / "agent" / "asr.py").read_text(encoding="utf-8")
    runtime_name = re.search(r'_SENSE_VOICE_MODEL\s*=\s*"([^"]+)"', asr_src)
    assert runtime_name, "agent/asr.py 里找不到 _SENSE_VOICE_MODEL"
    assert "k2-fsa/sherpa-onnx" in asr_src, "asr.py 的下载源变了——安装器也要跟着改"

    mod = _load(Path(__file__).resolve().parent)   # BASE 不影响这两个常量
    assert mod.ASR_MODEL_NAME == runtime_name.group(1), (
        "安装器 ASR_MODEL_NAME 与 agent/asr.py 不一致——"
        f"安装器 {mod.ASR_MODEL_NAME!r} vs 运行时 {runtime_name.group(1)!r}"
    )
    assert mod.ASR_MODEL_NAME in mod.ASR_MODEL_URL, "ASR_MODEL_URL 里应包含模型名"


def test_asr_model_is_prefetched_with_voice_feature():
    """勾了语音就该把 ASR 模型一起下好——否则用户第一次收到语音消息时才下 233M，卡在对话里"""
    mod = _load(Path(__file__).resolve().parent)
    specs = mod.MODELS.get(FEAT_VOICE, [])
    kinds = {s[2] for s in specs}
    assert "tarbz2" in kinds, "语音功能应预取 ASR 模型（kind=tarbz2）"


def test_wire_config_writes_backup_for_existing_config(tmp_path, monkeypatch):
    """改已有配置前必须备份——出问题能退回去"""
    mod = _load(tmp_path)
    _seed_example(tmp_path, mod)
    mod.bootstrap_config(dry_run=False)
    mod.wire_config([FEAT_MEMORY, FEAT_CONSOLE], fresh=True, dry_run=False)

    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    mod.wire_config([FEAT_MEMORY, FEAT_VOICE, FEAT_CONSOLE], fresh=False, dry_run=False)
    assert list(tmp_path.glob("config.yaml.bak-*")), "改已有配置前应留备份"
    assert _read_cfg(tmp_path)["voice"]["enabled"] is True
