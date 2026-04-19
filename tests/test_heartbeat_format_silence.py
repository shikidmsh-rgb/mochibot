"""Unit tests for _format_silence and the 消息 section of _build_observation_text."""

import pytest

from mochi.heartbeat import _format_silence, _build_observation_text


class TestFormatSilence:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "未知"),
            ("?", "未知"),
            ([], "未知"),
            (0, "刚刚"),
            (0.01, "刚刚"),
            (0.05, "3分钟前"),
            (0.5, "30分钟前"),
            (1.0, "1小时前"),
            (1.5, "1小时前"),
            (25.0, "1天前"),
            (-0.5, "刚刚"),
        ],
    )
    def test_format(self, value, expected):
        assert _format_silence(value) == expected


class TestObservationTextMessageSection:
    def test_message_section_uses_new_label_and_drops_user_status(self):
        obs = {"silence_hours": 0.05, "messages_today": 3}
        out = _build_observation_text(obs)
        assert "## 消息" in out
        assert "用户上次开口:" in out
        assert "3分钟前" in out
        assert "用户状态:" not in out
        assert "沉默时长:" not in out

    def test_message_section_handles_missing_silence(self):
        obs = {"messages_today": 0}
        out = _build_observation_text(obs)
        assert "用户上次开口: 未知" in out
