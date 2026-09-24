import io
import zipfile

from mochi.file_text import MAX_FILE_TEXT_CHARS, extract_file_text


def _docx(*paragraphs: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f"<w:p><w:r><w:t>{text[:2]}</w:t></w:r><w:r><w:t>{text[2:]}</w:t></w:r></w:p>"
        for text in paragraphs
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>',
        )
    return buffer.getvalue()


def test_reads_plain_text_word_and_truncates():
    assert extract_file_text("notes.csv", "名字,次数\n年糕,3".encode("gb18030")).text == (
        "名字,次数\n年糕,3"
    )
    assert extract_file_text("plan.docx", _docx("第一段文字", "第二段")).text == (
        "第一段文字\n第二段"
    )
    long = extract_file_text("long.txt", ("字" * (MAX_FILE_TEXT_CHARS + 1)).encode())
    assert long.truncated and len(long.text) == MAX_FILE_TEXT_CHARS
    assert not extract_file_text("short.txt", b"hello").truncated


def test_reports_unreadable_states():
    assert extract_file_text("photo.png", b"\x89PNG\r\n\x1a\n\x00\x00").status == "unsupported"
    assert extract_file_text("broken.pdf", b"%PDF-1.7 broken").status == "unreadable"
    assert extract_file_text("broken.docx", b"not a zip").status == "unreadable"
    assert extract_file_text("blank.txt", b"  \n").status == "no_text"
