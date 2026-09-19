"""不可信 QQ 附件的资源与格式边界。"""

import sys
import types
import zipfile

from agent import file_reader


def test_unknown_extension_is_rejected_instead_of_decoded_as_text(tmp_path):
    path = tmp_path / "payload.exe"
    path.write_bytes(b"MZ\x00\x01not-text")

    result = file_reader.extract_text(path)

    assert "不支持的格式" in result


def test_file_size_limit_applies_before_reading(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    path.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(file_reader, "MAX_FILE_BYTES", 4, raising=False)

    result = file_reader.extract_text(path)

    assert "文件过大" in result


def test_extracted_text_has_hard_character_limit(tmp_path, monkeypatch):
    path = tmp_path / "long.txt"
    path.write_text("一二三四五六七八", encoding="utf-8")
    monkeypatch.setattr(file_reader, "MAX_EXTRACTED_CHARS", 5, raising=False)

    result = file_reader.extract_text(path)

    assert result.startswith("一二三四五")
    assert "已截断" in result
    assert "六七八" not in result


def test_pdf_reader_stops_at_page_limit(tmp_path, monkeypatch):
    path = tmp_path / "many.pdf"
    path.write_bytes(b"fake-pdf")
    monkeypatch.setattr(file_reader, "MAX_PDF_PAGES", 2, raising=False)

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Pdf:
        pages = [Page("第一页"), Page("第二页"), Page("第三页")]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _p: Pdf()))

    result = file_reader._read_pdf(path)

    assert "第一页" in result and "第二页" in result
    assert "第三页" not in result
    assert "页数上限" in result


def test_docx_rejects_large_uncompressed_archive(tmp_path, monkeypatch):
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "0123456789")
    monkeypatch.setattr(
        file_reader, "MAX_DOCX_UNCOMPRESSED_BYTES", 5, raising=False,
    )

    result = file_reader._read_docx(path)

    assert "解压后过大" in result

