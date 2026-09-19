"""
知识库混合检索测试（2026-08-15，市场标准：hybrid + RRF + cross-encoder 重排）

- 搜词扩展：文档「> 搜词：」行变成查询扩展索引——停用词死查询（「糖糖你会什么」）有救
- RRF 融合：关键词/语义/扩展三路按排名融合（k=60）
- 重排门槛：reranker logit ≤ 0 视为不相关，全负诚实返回空
- .txt 加载：千恋万花_丛林.txt 此前被 rglob("*.md") 忽略，从未加载
- 降级链：无 reranker 时走旧顺序逻辑（行为不变，旧测试仍绿）
"""

import numpy as np

from agent.knowledge import KnowledgeBase


class FakeEmbed:
    """确定性伪向量引擎——64 维字符 one-hot。无关文本相似度 < 0.35，同主题文本有重叠"""
    ready = True

    def encode(self, text):
        v = np.zeros(64, dtype=np.float32)
        for ch in text:
            v[ord(ch) % 64] += 1.0
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def encode_batch(self, texts):
        return [self.encode(t) for t in texts]


class FakeReranker:
    """按候选顺序返回预设分数"""
    ready = True

    def __init__(self, scores):
        self._scores = scores

    def rerank(self, query, candidates, top_k=3):
        idx = list(range(len(candidates)))
        scored = [(i, self._scores[i] if i < len(self._scores) else -1.0) for i in idx]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


def _make_kb(tmp_path):
    (tmp_path / "糖糖功能.md").write_text(
        "# 糖糖功能\n> 搜词：你会什么 本领 技能 功能\n\n## 功能\n我能唱歌、查天气和翻译\n\n## 限制\n我不是万能的\n",
        encoding="utf-8")
    (tmp_path / "minecraft.md").write_text(
        "## 安装\n用启动器下载\n\n## 玩法\n合成需要工作台\n", encoding="utf-8")
    (tmp_path / "剧情.txt").write_text(
        "## 丛雨\n千恋万花的青梅竹马\n", encoding="utf-8")
    return KnowledgeBase(str(tmp_path))


class TestExpansion:
    def test_stopword_dead_query_rescued(self, tmp_path):
        """「糖糖你会什么」全部关键词是停用词——搜词扩展轮必须救回糖糖功能.md"""
        kb = _make_kb(tmp_path)
        r = kb.search("糖糖你会什么")
        assert "糖糖功能" in r
        assert "唱歌" in r

    def test_expansion_multi_terms(self, tmp_path):
        kb = _make_kb(tmp_path)
        assert "糖糖功能" in kb.search("你有什么本领")

    def test_expansion_no_false_hit(self, tmp_path):
        kb = _make_kb(tmp_path)
        assert kb.search("完全无关的zzzz话题") == ""

    def test_expansion_isolates_same_stem_documents(self, tmp_path):
        """搜词命中一份同名文档时，不得把另一目录的块一起召回。"""
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        (first_dir / "guide.md").write_text(
            "> 搜词：只命中第一份\n\n## 第一份\n第一份内容\n",
            encoding="utf-8",
        )
        (second_dir / "guide.md").write_text(
            "## 第二份\n第二份内容\n",
            encoding="utf-8",
        )

        kb = KnowledgeBase(str(tmp_path))
        hits = kb._expansion_hits("只命中第一份")

        assert hits
        assert {chunk.relative_path for chunk in hits} == {"first/guide.md"}
        assert all("第二份" not in chunk.content for chunk in hits)


class TestTxtLoading:
    def test_txt_file_loaded(self, tmp_path):
        kb = _make_kb(tmp_path)
        r = kb.search("丛雨")
        assert "千恋万花" in r or "丛雨" in r


class TestFusedPipeline:
    def test_reranker_orders_results(self, tmp_path):
        """重排定序：reranker 首选的块排在最前"""
        kb = _make_kb(tmp_path)
        reranker = FakeReranker([2.0, -1.0, -1.0])
        r = kb.search("minecraft怎么玩", embed_engine=FakeEmbed(), reranker=reranker)
        assert "minecraft" in r

    def test_junk_query_returns_empty_via_evidence_gate(self, tmp_path):
        """answerability 由检索证据把关（关键词≥2/语义>0.55/搜词命中），
        reranker 只排序不定生死——2026-08-15 实测 reranker logit 整体负偏，
        logit 门槛会误杀正确结果"""
        kb = _make_kb(tmp_path)
        reranker = FakeReranker([1.0])
        assert kb.search("zzzz完全无关", embed_engine=FakeEmbed(), reranker=reranker) == ""

    def test_no_path_hits_returns_empty_even_with_reranker(self, tmp_path):
        kb = _make_kb(tmp_path)
        reranker = FakeReranker([1.0])
        assert kb.search("zzzz完全无关", embed_engine=FakeEmbed(), reranker=reranker) == ""

    def test_fused_merges_three_paths(self, tmp_path):
        """三路候选都进池：reranker 收到合并后的候选列表"""
        kb = _make_kb(tmp_path)
        seen = []
        class SpyReranker(FakeReranker):
            def rerank(self, query, candidates, top_k=3):
                seen.append(len(candidates))
                return super().rerank(query, candidates, top_k)
        kb.search("糖糖你会什么 安装", embed_engine=FakeEmbed(), reranker=SpyReranker([1.0]))
        assert seen and seen[0] >= 2  # 至少两条来源的块进入候选池

    def test_sequential_fallback_without_reranker(self, tmp_path):
        """reranker=None → 旧顺序路径（旧测试行为不回归）"""
        kb = _make_kb(tmp_path)
        assert "minecraft" in kb.search("minecraft怎么玩", embed_engine=None)
        assert kb.search("zzzz无关") == ""


class TestSmallToBig:
    def test_short_chunk_does_not_cross_same_stem_documents(self, tmp_path):
        """不同目录的同名文档不能被 small-to-big 当成同一文档。"""
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        (first_dir / "guide.md").write_text(
            "## 总纲\n第一份文档\n", encoding="utf-8",
        )
        (second_dir / "guide.md").write_text(
            "## 另一个总纲\n第二份文档\n", encoding="utf-8",
        )
        kb = KnowledgeBase(str(tmp_path))

        assert kb._next_chunk_of(kb.chunks[0]) is None

    def test_short_chunk_merged_with_next(self, tmp_path):
        """small-to-big：标题短块重排第一时，合并同文档下一块正文"""
        (tmp_path / "指南.md").write_text(
            "## 总纲\n简短介绍\n\n## 正文\n这里是详细的操作步骤，第一步怎么做第二步怎么做\n",
            encoding="utf-8")
        kb = KnowledgeBase(str(tmp_path))
        # 让 reranker 把「总纲」短块排第一
        class PreferFirst(FakeReranker):
            def rerank(self, query, candidates, top_k=3):
                idx = list(range(len(candidates)))
                return [(i, 1.0 if i == 0 else 0.5) for i in idx][:top_k]
        r = kb.search("总纲", embed_engine=FakeEmbed(), reranker=PreferFirst([1.0]))
        assert "简短介绍" in r
        assert "详细的操作步骤" in r  # 下一块正文被合并进来
