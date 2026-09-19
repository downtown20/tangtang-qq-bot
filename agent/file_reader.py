"""
📁 文件解析器 — 从各种格式的文件中提取文本

支持：.txt .md .docx .pdf .csv .json
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger("糖糖.FileReader")

TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".csv", ".json",
})
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".docx", ".pdf"}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 100_000
MAX_PDF_PAGES = 100
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024


def extract_text(file_path: str | Path) -> str:
    """从文件中提取文本。根据扩展名选择解析器。"""
    path = Path(file_path)
    if not path.exists():
        return ""

    ext = path.suffix.lower()
    name = path.name

    if ext not in SUPPORTED_EXTENSIONS:
        logger.info(f"📁 不支持的文件格式 {ext}: {name}")
        return f"[文件: {name} — 不支持的格式]"

    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return f"[文件: {name} — 文件过大，无法解析]"
    except OSError as e:
        logger.warning(f"📁 无法读取文件信息: {name} — {e}")
        return f"[文件: {name} — 无法解析内容]"

    try:
        if ext in TEXT_EXTENSIONS:
            text = _read_text(path)
        elif ext == ".docx":
            text = _read_docx(path)
        else:
            text = _read_pdf(path)

        if len(text) > MAX_EXTRACTED_CHARS:
            return text[:MAX_EXTRACTED_CHARS] + "\n\n[内容超过长度上限，已截断]"
        return text

    except Exception as e:
        logger.warning(f"📁 文件解析失败: {name} — {e}")
        return f"[文件: {name} — 无法解析内容]"

# 2026-08-15 接线审计：get_file_summary 从未被业务调用（read_document 技能自行
# 组装摘要）已删除——框架先行（CLAUDE.md 反模式#6）。

# ═══════════════════════════════════════
# 内部解析器
# ═══════════════════════════════════════


def _read_text(path: Path) -> str:
    """读取纯文本文件（尝试 UTF-8 → GBK 兜底）"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="gbk")
        except UnicodeDecodeError:
            return path.read_text(encoding="latin-1")


def _read_docx(path: Path) -> str:
    """读取 Word 文档"""
    try:
        with zipfile.ZipFile(path) as archive:
            unpacked_size = sum(info.file_size for info in archive.infolist())
        if unpacked_size > MAX_DOCX_UNCOMPRESSED_BYTES:
            return f"[文件: {path.name} — 解压后过大，无法解析]"
    except (OSError, zipfile.BadZipFile) as e:
        logger.warning(f"📁 DOCX 压缩包无效: {path.name} — {e}")
        return f"[文件: {path.name} — 无法解析内容]"

    try:
        from docx import Document
        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except ImportError:
        logger.warning("python-docx 未安装，无法读取 .docx 文件")
        return f"[需要安装 python-docx 才能读取 .docx 文件: pip install python-docx]"


def _read_pdf(path: Path) -> str:
    """读取 PDF 文档"""
    # 优先 pdfplumber（效果好），兜底 PyPDF2
    try:
        import pdfplumber
        texts = []
        with pdfplumber.open(str(path)) as pdf:
            pages = pdf.pages
            for page in pages[:MAX_PDF_PAGES]:
                t = page.extract_text()
                if t:
                    texts.append(t)
        if texts:
            text = "\n\n".join(texts)
            if len(pages) > MAX_PDF_PAGES:
                text += f"\n\n[PDF 超过页数上限，仅提取前 {MAX_PDF_PAGES} 页]"
            return text
    except ImportError:
        pass

    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(str(path))
        pages = reader.pages
        texts = [page.extract_text() or "" for page in pages[:MAX_PDF_PAGES]]
        text = "\n\n".join(t for t in texts if t)
        if len(pages) > MAX_PDF_PAGES:
            text += f"\n\n[PDF 超过页数上限，仅提取前 {MAX_PDF_PAGES} 页]"
        return text
    except ImportError:
        logger.warning("pdfplumber/PyPDF2 未安装，无法读取 .pdf 文件")
        return f"[需要安装 pdfplumber 或 PyPDF2 才能读取 .pdf 文件]"
