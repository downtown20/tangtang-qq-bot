"""
🌐 联网搜索 — 让糖糖能查实时信息
用 DuckDuckGo 免费 API，无需注册无需 Key

已注册为技能 (agent/skills.py)
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("糖糖.WebSearch")

# 注册为技能（延迟导入避免循环引用）
def _register():
    from .skills import register_skill
    @register_skill(
        "web_search",
        "从互联网获取实时信息。你（LLM）的知识截止于训练数据——今天的新闻、昨天的天气、刚才发生的热点，你都不知道。这个工具让你能回答需要最新信息的问题。",
        {"query": "搜索关键词，如'北京今天天气'、'2024诺贝尔奖得主'"}
    )
    async def _web_search_skill(query: str = "") -> str:
        searcher = WebSearcher()
        return await searcher.search(query)

# 2026-08-15 接线审计：should_search 及 4 个正则常量从未被业务调用——
# 关键词门控替 LLM 做搜索决策（CLAUDE.md 反模式#9）的遗留，已删除。
# 搜索时机由 LLM 通过 web_search 工具自主决定。

class WebSearcher:
    """DuckDuckGo 搜索器"""

    def __init__(self, timeout: float = 8.0, max_results: int = 3):
        self._timeout = timeout
        self._max_results = max_results

    async def search(self, query: str) -> str:
        """搜索并返回格式化文本。失败返回空字符串。"""
        import httpx

        # 被墙/海外后端用短超时，避免国内用户白等
        fast_timeout = min(self._timeout, 2.5)

        # 方案 A：Bing 搜索（国内首选，cn.bing.com 直连稳定）
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                url = f"https://cn.bing.com/search?q={query}&setlang=zh-cn"
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                })
                if resp.status_code == 200:
                    result = self._parse_bing_html(resp.text, query)
                    if result:
                        logger.info(f"🌐 搜索命中(Bing): {query[:40]} → {len(result)}字")
                        return result
        except Exception as e:
            logger.debug(f"Bing 搜索失败: {e}")

        # 方案 B：DuckDuckGo Instant Answer API（海外可用，国内被墙）
        try:
            async with httpx.AsyncClient(timeout=fast_timeout) as client:
                url = f"https://api.duckduckgo.com/?q={query}&format=json&no_html=1&skip_disambig=1"
                resp = await client.get(url, headers={"User-Agent": "TangTangBot/1.0"})
                if resp.status_code == 200:
                    data = resp.json()
                    result = self._format_ddg(data, query)
                    if result:
                        logger.info(f"🌐 搜索命中(API): {query[:40]} → {len(result)}字")
                        return result
        except Exception as e:
            logger.debug(f"DuckDuckGo API 失败: {e}")

        # 方案 C：DuckDuckGo HTML 搜索（海外可用，国内被墙）
        try:
            async with httpx.AsyncClient(timeout=fast_timeout) as client:
                url = f"https://html.duckduckgo.com/html/?q={query}"
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                if resp.status_code == 200:
                    result = self._parse_ddg_html(resp.text, query)
                    if result:
                        logger.info(f"🌐 搜索命中(HTML): {query[:40]} → {len(result)}字")
                        return result
        except Exception as e:
            logger.debug(f"DuckDuckGo HTML 失败: {e}")

        # 方案 D：SearXNG 公共实例（海外可用，国内被墙）
        searx_instances = [
            "https://search.saptiva.com",
            "https://search.smnz.de",
            "https://search.slash-dev.de",
        ]
        for instance in searx_instances:
            try:
                async with httpx.AsyncClient(timeout=fast_timeout) as client:
                    url = f"{instance}/search?q={query}&format=json&language=zh-CN"
                    resp = await client.get(url, headers={"User-Agent": "TangTangBot/1.0"})
                    if resp.status_code == 200:
                        data = resp.json()
                        results = data.get("results", [])
                        if results:
                            parts = []
                            for r in results[:self._max_results]:
                                snippet = re.sub(r'<[^>]+>', '', r.get("content", "") or r.get("snippet", ""))
                                if len(snippet) > 20:
                                    parts.append(f"· {snippet[:200]}")
                            if parts:
                                logger.info(f"🌐 搜索命中(SearXNG): {query[:40]} → {len(parts)}条")
                                return "\n".join(parts)
            except Exception:
                continue

        return ""

    def _format_ddg(self, data: dict, query: str) -> str:
        """格式化 DuckDuckGo API 结果"""
        parts = []

        # 摘要
        abstract = data.get("Abstract", "").strip()
        if abstract:
            parts.append(abstract)

        # 答案
        answer = data.get("Answer", "").strip()
        if answer and answer != abstract:
            parts.append(answer)

        # 相关主题
        topics = data.get("RelatedTopics", [])
        for t in topics[:self._max_results]:
            text = t.get("Text", "").strip() if isinstance(t, dict) else ""
            if text and text not in abstract:
                # 清理 HTML 链接
                text = re.sub(r'<a[^>]*>|</a>', '', text)
                parts.append(f"· {text}")

        # 信息框
        infobox = data.get("Infobox", {})
        if infobox and infobox.get("content"):
            for item in infobox["content"][:3]:
                label = item.get("label", "")
                value = item.get("value", "")
                if label and value:
                    parts.append(f"{label}: {value}")

        return "\n".join(parts[:8]) if parts else ""

    def _parse_bing_html(self, html: str, query: str) -> str:
        """从 Bing 搜索结果页提取摘要"""
        import html as _html

        parts = []

        # Bing 搜索结果在 <li class="b_algo"> 里，摘要用 <p> 或 class="b_caption" 里的文本
        # 方法1: 匹配 b_algo 块中的文本段落
        algo_blocks = re.findall(
            r'<li class="b_algo"[^>]*>(.*?)</li>',
            html, re.DOTALL
        )
        for block in algo_blocks[:self._max_results]:
            # 提取文本摘要（去掉 HTML 标签，解码实体）
            text = re.sub(r'<[^>]+>', ' ', block)
            text = _html.unescape(text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 30:
                parts.append(f"· {text[:300]}")

        if parts:
            return "\n".join(parts)

        # 方法2: 备用 — 直接提取所有 <p> 文本（降级方案）
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
        for p in paragraphs[:self._max_results * 3]:
            text = re.sub(r'<[^>]+>', ' ', p)
            text = _html.unescape(text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 40:
                parts.append(f"· {text[:300]}")
            if len(parts) >= self._max_results:
                break

        return "\n".join(parts) if parts else ""

    def _parse_ddg_html(self, html: str, query: str) -> str:
        """从 DuckDuckGo HTML 搜索结果页提取摘要"""
        # 提取 snippet 文本
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>',
            html, re.DOTALL
        )
        if not snippets:
            # 备用正则
            snippets = re.findall(
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                html, re.DOTALL
            )
        if not snippets:
            snippets = re.findall(
                r'class="result__snippet"[^>]*>([^<]+)',
                html
            )

        if snippets:
            parts = []
            for s in snippets[:self._max_results]:
                clean = re.sub(r'<[^>]+>', '', s).strip()
                if len(clean) > 20:
                    parts.append(f"· {clean}")
            return "\n".join(parts)

        return ""
