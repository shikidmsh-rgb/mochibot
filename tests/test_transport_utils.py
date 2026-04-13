"""Tests for mochi/transport/utils.py — shared transport utilities."""

from mochi.transport.utils import clean_reply_markers, split_text, split_bubbles


# ── clean_reply_markers ─────────────────────────────────────────────────────


class TestCleanReplyMarkers:

    def test_strips_image_file_marker(self):
        assert clean_reply_markers("hello [IMAGE_FILE:foo.png] world") == "hello  world"

    def test_strips_sticker_marker(self):
        assert clean_reply_markers("[STICKER:happy] hi") == "hi"

    def test_strips_skip_marker(self):
        assert clean_reply_markers("[SKIP]") == ""

    def test_strips_multiple_markers(self):
        text = "[STICKER:wave] hello [IMAGE_FILE:pic.jpg] world [SKIP]"
        assert clean_reply_markers(text) == "hello  world"

    def test_no_markers(self):
        assert clean_reply_markers("plain text here") == "plain text here"

    def test_empty_string(self):
        assert clean_reply_markers("") == ""


# ── split_text ──────────────────────────────────────────────────────────────


class TestSplitText:

    def test_within_limit(self):
        assert split_text("short", 100) == ["short"]

    def test_exact_limit(self):
        assert split_text("abcde", 5) == ["abcde"]

    def test_exceeds_limit(self):
        result = split_text("abcdefghij", 3)
        assert result == ["abc", "def", "ghi", "j"]

    def test_multibyte(self):
        text = "你好世界测试"  # 6 chars
        result = split_text(text, 4)
        assert result == ["你好世界", "测试"]

    def test_empty_string(self):
        assert split_text("", 100) == [""]


# ── split_bubbles ───────────────────────────────────────────────────────────


class TestSplitBubbles:

    def test_explicit_delimiter(self):
        result = split_bubbles("hello world ||| this is great ||| welcome back")
        assert result == ["hello world", "this is great", "welcome back"]

    def test_double_newline_fallback(self):
        result = split_bubbles("paragraph one\n\nparagraph two\n\nparagraph three")
        assert result == ["paragraph one", "paragraph two", "paragraph three"]

    def test_max_bubbles_cap(self):
        text = " ||| ".join(f"long part number {i}" for i in range(6))
        result = split_bubbles(text, max_bubbles=4)
        assert len(result) == 4

    def test_short_fragment_merge(self):
        # "ok" is 2 chars, below default min_chars=8
        result = split_bubbles("hello world ||| ok")
        assert len(result) == 1
        assert "ok" in result[0]

    def test_no_split_single_paragraph(self):
        text = "just one paragraph with no split points"
        assert split_bubbles(text) == [text]

    def test_empty_parts_filtered(self):
        result = split_bubbles("hello world |||  ||| welcome back")
        assert result == ["hello world", "welcome back"]

    def test_all_empty_parts(self):
        # All parts empty after strip → falls back since len(parts) <= 1
        result = split_bubbles("|||  |||")
        assert len(result) == 1
