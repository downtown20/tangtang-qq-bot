import sys
import types

from agent.embeddings import EmbeddingEngine


def test_load_switches_embedding_model_to_eval_mode(monkeypatch, tmp_path):
    """BGE 加载后必须关闭 dropout，保证同一查询的向量稳定。"""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    calls = []

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, _path):
            return cls()

    class FakeModel:
        @classmethod
        def from_pretrained(cls, _path):
            return cls()

        def eval(self):
            calls.append("eval")
            return self

    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(
        BertTokenizer=FakeTokenizer,
        BertModel=FakeModel,
    ))
    monkeypatch.setattr(EmbeddingEngine, "MODEL_PATHS", [str(tmp_path)])

    engine = EmbeddingEngine()
    engine.load()

    assert engine.ready is True
    assert calls == ["eval"]
