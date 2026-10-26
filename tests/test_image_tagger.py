# -*- coding: utf-8 -*-
"""图库打标（`agent/image_tagger.py`）—— 本地 Ollama / 云端 API 两种方式。

## 这份测试守什么

主人 2026-10-26：「图库没有调用模型打标签功能，该功能需要提供本地、API 两种方式，
本地模式我们只提供接口和必要的文件夹位置……然后将开关整合到桌面端
而不是用 `D:\\qq-小糖糖\\打标色图.bat`。」

所以守三条：

1. **不能悄悄降级**：没配好就明确说「缺什么」（`_provider_ready` 返回人话），
   而不是点一下就静默不动、或者用一个简化版解析器冒充。
2. **解析层只有一份**：复用 `tag_cg_ollama.py` 的 `make_rec_v2`；
   脚本不在时必须**报错**，不能退回「自己写的简化版」——那会产出质量不同的标签。
3. **改名字段要安全**：改名撞到已有文件时宁可不改，也不能覆盖另一张图。

⚠ 这里**不真调模型**（本机和云端都不联网）。真链路验证靠设置页的「试打一张」。
"""
import json
from pathlib import Path

import pytest

from agent import image_tagger as T


@pytest.fixture()
def gallery(tmp_path, monkeypatch):
    """临时图库：3 张图 + metadata.json，其中 1 张已带 persons。"""
    d = tmp_path / "gallery"
    (d / "_tag").mkdir(parents=True)
    for name in ("a_0001.jpg", "b_0001.jpg", "c_0001.jpg"):
        (d / name).write_bytes(b"\xff\xd8\xff\xd9")
    # ⚠ 一条**在 metadata 里但没有 persons** 的记录：这是最容易搞混的口径差——
    #    「有记录」不等于「打过标」（脚本 --resume 认的是 persons）。留着它，
    #    下面那条 scan 判据的变异才抓得住。
    (d / "metadata.json").write_text(
        json.dumps({
            "a_0001.jpg": {"persons": "单人", "tagged_at": "x"},
            "b_0001.jpg": {"desc": "", "tagged_at": "x"},
        }, ensure_ascii=False), encoding="utf-8")
    cfg = dict(T.DEFAULTS)
    cfg["dir"] = str(d)
    monkeypatch.setattr(T, "load_config", lambda: dict(cfg))
    T.reset_state_for_tests()
    return d, cfg


# ── 配置与路径 ────────────────────────────────────────────────

def test_defaults_are_off():
    """默认**不开**：没配过的人不该一进图库就看到能烧 GPU 的按钮。"""
    assert T.DEFAULTS["provider"] == "off"
    assert T.DEFAULTS["rename"] is True, "文件名参与她的语义索引，默认应与现有脚本一致"


def test_gallery_dir_relative_and_absolute(monkeypatch):
    cfg = dict(T.DEFAULTS)
    cfg["dir"] = "stickers_cg"
    assert T.gallery_dir(cfg) == T.PROJECT_ROOT / "stickers_cg"
    abs_dir = T.PROJECT_ROOT / "某个图库"
    cfg["dir"] = str(abs_dir)
    assert T.gallery_dir(cfg) == abs_dir


def test_load_config_merges_yaml_section(tmp_path, monkeypatch):
    """`config.yaml` 只写一部分时，其余回落默认（不是整份丢掉）。"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "image_tagging:\n  provider: ollama\n  ollama_model: test-vl:1b\n",
        encoding="utf-8")
    monkeypatch.setattr(T, "PROJECT_ROOT", tmp_path)
    cfg = T.load_config()
    assert cfg["provider"] == "ollama"
    assert cfg["ollama_model"] == "test-vl:1b"
    assert cfg["max_side"] == T.DEFAULTS["max_side"], "没写的字段必须回落默认"


def test_load_config_survives_broken_yaml(tmp_path, monkeypatch):
    """配置写坏了 → 回落默认，**不能抛**（图库页要能打开）。"""
    (tmp_path / "config.yaml").write_text("image_tagging: [未闭合\n", encoding="utf-8")
    monkeypatch.setattr(T, "PROJECT_ROOT", tmp_path)
    assert T.load_config()["provider"] == T.DEFAULTS["provider"]


# ── 扫描口径 ──────────────────────────────────────────────────

def test_scan_counts_match_resume_semantics(gallery):
    """「已打标」= metadata 里有非空 `persons`，与脚本 `--resume` 同口径。"""
    _, _ = gallery
    sc = T.scan()
    assert sc["files"] == 3
    assert sc["tagged"] == 1, "只有带 persons 的才算打过标（b 有记录但没 persons）"
    assert sc["pending"] == 2
    assert sc["metadata_entries"] == 2


def test_scan_of_missing_dir_is_zero_not_crash(gallery):
    d, cfg = gallery
    cfg["dir"] = str(d / "不存在")
    sc = T.scan(cfg)
    assert sc["dir_exists"] is False
    assert sc["files"] == 0 and sc["pending"] == 0


# ── 「为什么不能跑」要说清楚 ─────────────────────────────────

@pytest.mark.parametrize("provider,patch,expect_in", [
    ("off", {}, "还没选打标方式"),
    ("ollama", {"ollama_model": ""}, "模型名是空的"),
    ("ollama", {"ollama_host": ""}, "地址是空的"),
    ("api", {"api_model": ""}, "API 模型名是空的"),
    ("api", {"api_key": ""}, "API key 没配"),
    ("weird", {}, "未知的打标方式"),
])
def test_provider_ready_explains_what_is_missing(provider, patch, expect_in, monkeypatch):
    monkeypatch.delenv("IMAGE_TAG_API_KEY", raising=False)
    cfg = dict(T.DEFAULTS)
    cfg.update({"provider": provider, **patch})
    why = T._provider_ready(cfg)
    assert expect_in in why, why


def test_provider_ready_ok_for_complete_config(monkeypatch):
    monkeypatch.setenv("IMAGE_TAG_API_KEY", "sk-test")
    cfg = dict(T.DEFAULTS)
    cfg.update({"provider": "api", "api_model": "qwen-vl-max"})
    assert T._provider_ready(cfg) == ""
    cfg.update({"provider": "ollama"})
    assert T._provider_ready(cfg) == ""


# ── 跑一轮：入口护栏 ─────────────────────────────────────────

def test_run_refuses_when_not_configured(gallery):
    """没选方式 → 明确拒绝，且**不启动线程**（否则界面显示「在跑」但什么都没发生）。"""
    out = T.run()
    assert out["ok"] is False
    assert "还没选打标方式" in out["error"]
    assert T.status()["progress"]["running"] is False


def test_run_says_no_work_when_all_tagged(gallery):
    d, cfg = gallery
    (d / "b_0001.jpg").unlink()
    (d / "c_0001.jpg").unlink()
    cfg["provider"] = "api"
    cfg["api_model"] = "m"
    cfg["api_key"] = "sk-test"   # 配置得「能跑」，才能测到「没活干」那一步
    T.load_config = lambda: dict(cfg)  # noqa: ARG005  (fixture 已 patch，这里确保生效)
    out = T.run()
    assert out["ok"] is True and out["started"] is False
    assert "没有需要打标的图" in out["message"]


def test_cancel_when_idle_is_harmless():
    T.reset_state_for_tests()
    out = T.cancel()
    assert out["ok"] is True and out["running"] is False


def test_status_hides_api_key(gallery):
    _, cfg = gallery
    cfg["api_key"] = "sk-super-secret"
    T.load_config = lambda: dict(cfg)  # noqa: ARG005
    st = T.status()
    assert "api_key" not in st["config"], "接口不能把 key 原样吐回前端"
    assert st["config"]["api_key_configured"] is True


# ── 解析层必须复用脚本（不许自己写简化版）───────────────────

def test_tagger_missing_script_raises_instead_of_faking(gallery, monkeypatch, tmp_path):
    """两处都没有脚本 → **报错**，不能退回一个「简化解析器」假装能打标。"""
    T.reset_state_for_tests()
    monkeypatch.setattr(T, "FALLBACK_SCRIPT_ROOT", tmp_path / "没有回落的库")
    with pytest.raises(RuntimeError) as ei:
        T._tagger()
    assert "找不到打标脚本" in str(ei.value)


def test_script_falls_back_to_the_shared_parser(tmp_path):
    """贴图库没有 `_tag/` → 回落到 stickers_cg 的**同一份**脚本（解析层只有一份）。"""
    script = T._script_for(tmp_path)
    assert script is not None, "没有回落路径，贴图库就永远不能打标"
    assert T.FALLBACK_SCRIPT_ROOT in script.parents, "回落到了别的脚本（可能是简化版）"


def test_tagger_loads_the_real_script():
    """真项目里能加载到脚本，并且暴露三件必需的东西。"""
    T.reset_state_for_tests()
    mod = T._tagger()
    assert callable(mod.make_rec_v2)
    assert callable(mod._encode_image)
    assert isinstance(mod.PROMPT, str) and mod.PROMPT.strip()


def test_make_rec_v2_produces_the_fields_she_reads():
    """她（`agent/sticker.py`）读 `emotions` / `scene_tags` / `desc`，一个都不能少。"""
    T.reset_state_for_tests()
    rec, new_name = T._tagger().make_rec_v2(
        "后入_露乳_1a1d.jpg", "单人，女性，露出胸部，表情害羞", {})
    for key in ("persons", "act", "emotions", "scene_tags", "desc", "tagged_at"):
        assert key in rec, f"记录缺字段 {key}——她会读不到"
    assert new_name.endswith(".jpg") and "_1a1d" in new_name, new_name


# ── 两种 provider 的错误路径（不联网）────────────────────────

def test_api_without_key_names_the_env_var(monkeypatch):
    monkeypatch.delenv("IMAGE_TAG_API_KEY", raising=False)
    cfg = dict(T.DEFAULTS)
    cfg.update({"provider": "api", "api_key": ""})
    with pytest.raises(RuntimeError) as ei:
        T._ask(cfg, "AAAA", "p")
    assert "IMAGE_TAG_API_KEY" in str(ei.value), "报错要告诉他 key 该放哪"


def test_ollama_connection_failure_is_explained():
    """连不上 Ollama 时要说「连不上 地址」，不能抛裸 URLError。"""
    cfg = dict(T.DEFAULTS)
    cfg.update({"provider": "ollama", "ollama_host": "http://127.0.0.1:1",
                "timeout_s": 2})
    with pytest.raises(RuntimeError) as ei:
        T._ask(cfg, "AAAA", "p")
    assert "连不上" in str(ei.value)


def test_unknown_provider_rejected():
    cfg = dict(T.DEFAULTS)
    cfg["provider"] = "openai"
    with pytest.raises(RuntimeError) as ei:
        T._ask(cfg, "AAAA", "p")
    assert "未知的打标方式" in str(ei.value)


# ── describe_one（设置页「试打一张」）──────────────────────

def test_describe_one_rejects_path_escape(gallery):
    d, cfg = gallery
    (d.parent / "secret.txt").write_text("x", encoding="utf-8")
    cfg["provider"] = "ollama"
    out = T.describe_one("../secret.txt")
    assert out["ok"] is False
    assert "不在这图库" in out["error"]


def test_describe_one_reports_missing_file(gallery):
    _, cfg = gallery
    cfg["provider"] = "ollama"
    out = T.describe_one("nope.jpg")
    assert out["ok"] is False and "没有这张图" in out["error"]


# ── 改名安全 ─────────────────────────────────────────────────

def test_worker_does_not_overwrite_on_rename_collision(gallery, monkeypatch):
    """改名目标已存在 → 这一张算失败，**绝不覆盖另一张图**。"""
    d, cfg = gallery
    cfg.update({"provider": "ollama", "rename": True})
    # 目标名已存在，而且**内容与源图不同**——这样才能验出「有没有被覆盖」
    (d / "撞名_0001.jpg").write_bytes(b"\x00\x01\x02")
    T.reset_state_for_tests()

    class _Fake:
        PROMPT = "p"

        @staticmethod
        def _encode_image(path, max_side=768):
            return "AAAA"

        @staticmethod
        def make_rec_v2(name, desc, old):
            return {"persons": "单人"}, "撞名_0001.jpg"

    monkeypatch.setattr(T, "_tagger", lambda dir_path=None: _Fake)
    monkeypatch.setattr(T, "_ask", lambda cfg, b64, prompt: "描述")
    T._worker(cfg, [d / "b_0001.jpg"])
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    # 1) 目标文件**一个字节都没被动**（覆盖了就再也找不回来）
    assert (d / "撞名_0001.jpg").read_bytes() == b"\x00\x01\x02", "撞名目标被覆盖了"
    # 2) 也没有把记录挪到撞名上
    assert meta.get("撞名_0001.jpg") is None
    # 3) 这一张记为失败（不是「成功改名」）
    assert T.status()["progress"]["fail"] == 1


# ═══════════════════════════════════════════════════════════════
# 「打哪些图 / 怎么打」由**图库页调用时传入**（2026-10-27）
#
# 主人原话：「设置中的『图库打标』仅仅是调整背后参数、信息、模型的而非决定打标方式，
# 打标方式，选哪些图打标需要在图库中具体设置并打标。」
#
# 所以守：dir / rename / overwrite / scope 四个参数**传了就得生效**，
# 且不传时回落 config（老配置不许失效）。
# ═══════════════════════════════════════════════════════════════

def _two_galleries(tmp_path, monkeypatch):
    """两个库：A 有 3 张（a1 已打标），B 有 1 张。验 dir 真的按调用方走。"""
    a = tmp_path / "gallery_a"
    b = tmp_path / "gallery_b"
    for d in (a, b):
        (d / "_tag").mkdir(parents=True)
    for name in ("a1.jpg", "a2.jpg", "a3.jpg"):
        (a / name).write_bytes(b"\xff\xd8\xff\xd9")
    (b / "b1.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (a / "metadata.json").write_text(
        json.dumps({"a1.jpg": {"persons": "单人"}}), encoding="utf-8")
    cfg = dict(T.DEFAULTS)
    cfg.update({"provider": "ollama"})
    monkeypatch.setattr(T, "load_config", lambda: dict(cfg))
    T.reset_state_for_tests()
    return a, b, cfg


class _SyncThread:
    """把 run() 的后台线程变成同步执行——测试要能确定性地看到 _worker 收到什么。"""

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


class _FakeTagger:
    PROMPT = "p"

    @staticmethod
    def _encode_image(path, max_side=768):
        return "AAAA"

    @staticmethod
    def make_rec_v2(name, desc, old):
        return {"persons": "单人"}, "改名_" + name


def _patch_model(monkeypatch):
    monkeypatch.setattr(T, "_tagger", lambda dir_path=None: _FakeTagger)
    monkeypatch.setattr(T, "_ask", lambda cfg, b64, prompt: "描述")


def test_dir_passed_by_caller_wins_over_config(tmp_path, monkeypatch):
    """图库页传 A 库 → 只打 A 库；config 里的 dir 不再是唯一真相。"""
    a, b, cfg = _two_galleries(tmp_path, monkeypatch)
    assert [p.name for p in T._select_todo(cfg, str(a), "pending", None, False, 0)] \
        == ["a2.jpg", "a3.jpg"], "pending 应跳过已打标的 a1"
    assert [p.name for p in T._select_todo(cfg, str(b), "pending", None, False, 0)] \
        == ["b1.jpg"]
    # scan 也要跟着走，否则按钮显示的「还剩几张」是另一个库的数字
    assert T.scan(dir_path=str(a))["files"] == 3
    assert T.scan(dir_path=str(b))["files"] == 1


def test_scope_all_can_overwrite_and_pending_cannot(tmp_path, monkeypatch):
    """`all` + overwrite=True → 连已打标的也打；`pending` 永远跳过已打标的。"""
    a, _, cfg = _two_galleries(tmp_path, monkeypatch)
    assert len(T._select_todo(cfg, str(a), "all", None, True, 0)) == 3
    # all + overwrite=False 等价于 pending（同一判据、同一实现）
    assert [p.name for p in T._select_todo(cfg, str(a), "all", None, False, 0)] \
        == ["a2.jpg", "a3.jpg"]
    # run() 里 pending 强制忽略 overwrite（防止「只打未打标的」还去重打）
    _patch_model(monkeypatch)
    monkeypatch.setattr(T.threading, "Thread", _SyncThread)
    out = T.run(dir_path=str(a), scope="pending", overwrite=True)
    assert out["ok"] is True and out["total"] == 2, out
    assert out["overwrite"] is False


def test_scope_selected_only_takes_named_files(tmp_path, monkeypatch):
    """「只打选中的图」只认传进来的文件名——多一个都不打。"""
    a, _, cfg = _two_galleries(tmp_path, monkeypatch)
    todo = T._select_todo(cfg, str(a), "selected", ["a1.jpg", "a3.jpg", "不存在.jpg"],
                          False, 0)
    assert [p.name for p in todo] == ["a3.jpg"], "a1 已打标应跳过；不存在的名字应忽略"
    todo = T._select_todo(cfg, str(a), "selected", ["a1.jpg", "a3.jpg"], True, 0)
    assert [p.name for p in todo] == ["a1.jpg", "a3.jpg"]


def test_run_rejects_unknown_scope(tmp_path, monkeypatch):
    a, _, _ = _two_galleries(tmp_path, monkeypatch)
    out = T.run(dir_path=str(a), scope="随便什么")
    assert out["ok"] is False and "未知的打标范围" in out["error"]


def test_status_reports_where_it_would_run(tmp_path, monkeypatch):
    """status 必须带回「在哪个目录 / 能不能跑」，否则界面没法把话说清楚。"""
    a, _, _ = _two_galleries(tmp_path, monkeypatch)
    st = T.status(dir_path=str(a), source="cg")
    assert st["source"] == "cg"
    assert st["dir"] == str(a)
    assert st["scan"]["dir"] == str(a)
    assert st["dir_ready"] == "", st["dir_ready"]
    missing = T.status(dir_path=str(a / "不存在"))
    assert "不存在" in missing["dir_ready"]


def test_scan_many_sums_libraries(tmp_path, monkeypatch):
    """合集（图库页「贴图」）的合计只在这里算一次，bridge 不许再写一遍加法。"""
    a, b, cfg = _two_galleries(tmp_path, monkeypatch)
    out = T.scan_many(cfg, [a, b])
    assert out["files"] == 4 and out["tagged"] == 1 and out["pending"] == 3
    assert out["dir"] is None and len(out["dirs"]) == 2
    st = T.status(scan_dirs=[a, b], source="sticker_all")
    assert st["scan"]["files"] == 4 and st["dir"] is None


def test_worker_honours_rename_flag_and_writes_only_the_target_dir(tmp_path, monkeypatch):
    """rename=False 就不改名；写盘只写 dir_path 那个库（不碰另一个库）。"""
    a, b, cfg = _two_galleries(tmp_path, monkeypatch)
    _patch_model(monkeypatch)
    T._worker(cfg, [a / "a2.jpg"], str(a), False)
    meta_a = json.loads((a / "metadata.json").read_text(encoding="utf-8"))
    assert "a2.jpg" in meta_a, "rename=False 时不该改名"
    assert "改名_a2.jpg" not in meta_a
    assert (a / "a2.jpg").exists()
    assert not (b / "metadata.json").exists(), "写了另一个库的 metadata（dir 没生效）"

    T.reset_state_for_tests()
    T._worker(cfg, [a / "a3.jpg"], str(a), True)
    meta_a = json.loads((a / "metadata.json").read_text(encoding="utf-8"))
    assert "改名_a3.jpg" in meta_a and not (a / "a3.jpg").exists()
