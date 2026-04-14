"""Tests for mochi.error_buffer — ring buffer + diagnostic report."""

import logging
import time

import pytest


# ── BufferHandler + ring buffer ─────────────────────────────────────────

class TestBufferHandler:
    """BufferHandler captures WARNING+ records into the ring buffer."""

    def setup_method(self):
        from mochi.error_buffer import _buffer, BufferHandler
        _buffer.clear()
        self.handler = BufferHandler()
        self.logger = logging.getLogger("mochi.test_buffer")
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def teardown_method(self):
        self.logger.removeHandler(self.handler)
        from mochi.error_buffer import _buffer
        _buffer.clear()

    def test_captures_warning(self):
        from mochi.error_buffer import _buffer
        self.logger.warning("test warning")
        assert len(_buffer) == 1
        assert _buffer[0]["level"] == "WARNING"
        assert _buffer[0]["message"] == "test warning"
        assert _buffer[0]["name"] == "mochi.test_buffer"

    def test_captures_error(self):
        from mochi.error_buffer import _buffer
        self.logger.error("test error")
        assert len(_buffer) == 1
        assert _buffer[0]["level"] == "ERROR"

    def test_ignores_info(self):
        from mochi.error_buffer import _buffer
        self.logger.info("should be ignored")
        assert len(_buffer) == 0

    def test_ignores_debug(self):
        from mochi.error_buffer import _buffer
        self.logger.debug("should be ignored")
        assert len(_buffer) == 0

    def test_captures_traceback(self):
        from mochi.error_buffer import _buffer
        try:
            raise ValueError("boom")
        except ValueError:
            self.logger.error("caught error", exc_info=True)
        assert len(_buffer) == 1
        assert _buffer[0]["traceback"] is not None
        assert "ValueError" in _buffer[0]["traceback"]

    def test_no_traceback_when_no_exc(self):
        from mochi.error_buffer import _buffer
        self.logger.error("no traceback here")
        assert len(_buffer) == 1
        assert _buffer[0]["traceback"] is None

    def test_ring_buffer_bounded(self):
        from mochi.error_buffer import _buffer
        for i in range(600):
            self.logger.warning("msg %d", i)
        assert len(_buffer) == 500  # maxlen


# ── get_recent_errors ───────────────────────────────────────────────────

class TestGetRecentErrors:

    def setup_method(self):
        from mochi.error_buffer import _buffer
        _buffer.clear()

    def teardown_method(self):
        from mochi.error_buffer import _buffer
        _buffer.clear()

    def test_returns_recent(self):
        from mochi.error_buffer import _buffer, get_recent_errors
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _buffer.append({"time": now, "level": "ERROR", "name": "test",
                        "message": "recent", "traceback": None})
        assert len(get_recent_errors(24)) == 1

    def test_filters_old(self):
        from mochi.error_buffer import _buffer, get_recent_errors
        _buffer.append({"time": "2020-01-01 00:00:00", "level": "ERROR",
                        "name": "test", "message": "old", "traceback": None})
        assert len(get_recent_errors(24)) == 0


# ── _mask ───────────────────────────────────────────────────────────────

class TestMask:

    def test_empty(self):
        from mochi.error_buffer import _mask
        assert _mask("") == "(not set)"

    def test_short(self):
        from mochi.error_buffer import _mask
        assert _mask("abc") == "***"

    def test_long(self):
        from mochi.error_buffer import _mask
        result = _mask("sk-1234567890abcdef")
        assert result.startswith("sk-")
        assert "***" in result
        assert result.endswith("def")

    def test_none_like(self):
        from mochi.error_buffer import _mask
        assert _mask(None) == "(not set)"


# ── register_log_source ────────────────────────────────────────────────

class TestRegisterLogSource:

    def test_registers(self):
        from mochi.error_buffer import register_log_source, _log_source
        fn = lambda: ["line1", "line2"]
        register_log_source(fn)
        from mochi import error_buffer
        assert error_buffer._log_source is fn
        # cleanup
        error_buffer._log_source = None


# ── get_diagnostic_report ───────────────────────────────────────────────

class TestDiagnosticReport:

    def setup_method(self):
        from mochi.error_buffer import _buffer
        _buffer.clear()

    def teardown_method(self):
        from mochi.error_buffer import _buffer
        _buffer.clear()
        from mochi import error_buffer
        error_buffer._log_source = None

    def test_report_contains_header(self):
        from mochi.error_buffer import get_diagnostic_report
        report = get_diagnostic_report()
        assert "MochiBot Diagnostic Report" in report

    def test_report_contains_system_info(self):
        from mochi.error_buffer import get_diagnostic_report
        report = get_diagnostic_report()
        assert "System Info" in report
        assert "Python:" in report
        assert "Platform:" in report

    def test_report_contains_config(self):
        from mochi.error_buffer import get_diagnostic_report
        report = get_diagnostic_report()
        assert "Config Summary" in report
        assert "CHAT_PROVIDER:" in report

    def test_report_masks_api_key(self):
        from mochi.error_buffer import get_diagnostic_report
        report = get_diagnostic_report()
        # API key should be masked or "(not set)", not raw
        assert "CHAT_API_KEY:" in report
        line = [l for l in report.split("\n") if "CHAT_API_KEY:" in l][0]
        value = line.split(":", 1)[1].strip()
        assert value in ("(not set)", ) or "***" in value

    def test_report_contains_database(self):
        from mochi.error_buffer import get_diagnostic_report
        report = get_diagnostic_report()
        assert "Database" in report

    def test_report_contains_errors_section(self):
        from mochi.error_buffer import get_diagnostic_report
        report = get_diagnostic_report()
        assert "Recent Errors" in report

    def test_report_includes_log_source(self):
        from mochi.error_buffer import register_log_source, get_diagnostic_report
        register_log_source(lambda: ["test log line 1", "test log line 2"])
        report = get_diagnostic_report()
        assert "test log line 1" in report
        assert "test log line 2" in report

    def test_report_no_log_source(self):
        from mochi.error_buffer import get_diagnostic_report
        report = get_diagnostic_report()
        assert "log source not registered" in report
