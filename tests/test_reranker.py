import json

from agent import reranker as reranker_module


def _patch_transformers(monkeypatch, model_cls):
    class Tokenizer:
        @classmethod
        def from_pretrained(cls, _path):
            return cls()

    class Transformers:
        AutoTokenizer = Tokenizer
        AutoModelForSequenceClassification = model_cls

    monkeypatch.setitem(__import__("sys").modules, "transformers", Transformers)


def test_load_uses_low_cpu_mem_usage_when_supported(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"hidden_size": 2}), encoding="utf-8")
    calls = []

    class Model:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls.append((path, kwargs))
            return cls()

        def eval(self):
            return self

        def to(self, _device):
            return self

    _patch_transformers(monkeypatch, Model)
    monkeypatch.setattr(reranker_module, "RERANKER_PATHS", [str(tmp_path)])
    monkeypatch.setattr(reranker_module, "_reranker_runtime_check", lambda: (True, ""))
    engine = reranker_module.RerankerEngine()

    engine.load()

    assert engine.ready is True
    assert calls and calls[0][1]["low_cpu_mem_usage"] is True


def test_load_failure_clears_partial_model_and_records_error(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    class FailingModel:
        @classmethod
        def from_pretrained(cls, _path, **_kwargs):
            raise OSError("page file too small")

    _patch_transformers(monkeypatch, FailingModel)
    monkeypatch.setattr(reranker_module, "RERANKER_PATHS", [str(tmp_path)])
    monkeypatch.setattr(reranker_module, "_reranker_runtime_check", lambda: (True, ""))
    engine = reranker_module.RerankerEngine()

    engine.load()

    assert engine.ready is False
    assert engine._model is None
    assert engine._tokenizer is None
    assert "page file" in engine.last_load_error


def test_load_skips_unsupported_runtime_before_model_load(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    class UnexpectedModel:
        @classmethod
        def from_pretrained(cls, *_args, **_kwargs):
            raise AssertionError("unsupported runtime must not load the model")

    _patch_transformers(monkeypatch, UnexpectedModel)
    monkeypatch.setattr(reranker_module, "RERANKER_PATHS", [str(tmp_path)])
    monkeypatch.setattr(
        reranker_module,
        "_reranker_runtime_check",
        lambda: (False, "不支持的 PyTorch 开发版运行时: 2.14.0.dev"),
    )
    engine = reranker_module.RerankerEngine()

    engine.load()

    assert engine.ready is False
    assert "开发版" in engine.last_load_error
