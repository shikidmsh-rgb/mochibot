"""Extract readable text from a file the owner sent for the current turn."""

import io
import zipfile
from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree

MAX_FILE_TEXT_CHARS = 20000

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True)
class FileText:
    status: Literal["ok", "unsupported", "no_text", "unreadable"]
    text: str = ""
    truncated: bool = False


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("encrypted PDF")
    parts: list[str] = []
    size = 0
    for page in reader.pages:
        part = page.extract_text() or ""
        parts.append(part)
        size += len(part)
        if size > MAX_FILE_TEXT_CHARS:
            break
    return "\n".join(parts)


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = (
        "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t"))
        for paragraph in root.iter(f"{_WORD_NS}p")
    )
    return "\n".join(paragraphs)


def _plain_text(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def extract_file_text(name: str, data: bytes) -> FileText:
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        if data.startswith(b"%PDF-"):
            text = _pdf_text(data)
        elif suffix == "docx":
            text = _docx_text(data)
        else:
            text = _plain_text(data)
            if text is None:
                return FileText("unsupported")
    except Exception:
        return FileText("unreadable")
    text = text.strip()
    if not text:
        return FileText("no_text")
    return FileText(
        "ok", text[:MAX_FILE_TEXT_CHARS], truncated=len(text) > MAX_FILE_TEXT_CHARS,
    )
