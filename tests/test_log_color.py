"""Focused tests for log.ColoredFormatter and the log.log factory.

Color goes ONLY to the stdout/StreamHandler; the rotating file logs stay plain
(no ANSI). Color is gated by a flag/NO_COLOR env, never by isatty (the node's
stdout is a journald pipe under systemd).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import log  # noqa: E402

ESC = "\033["


def _record(level, msg="hello"):
    return logging.LogRecord(
        name="root", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None, func="some_func",
    )


def test_colored_formatter_emits_ansi_for_warning_and_error():
    fmt = log.ColoredFormatter("%(asctime)s %(levelname)s %(message)s", use_color=True)
    for level in (logging.WARNING, logging.ERROR, logging.CRITICAL):
        out = fmt.format(_record(level, "Status: x"))
        assert ESC in out, "expected ANSI escape for level {}".format(level)
        assert out.endswith(log.RESET), "color must be reset so it does not bleed"
        # the level name appears (severity visible at a glance)
        assert logging.getLevelName(level) in out


def test_colored_formatter_colors_level_for_info_and_debug():
    fmt = log.ColoredFormatter("%(asctime)s %(levelname)s %(message)s", use_color=True)
    for level in (logging.DEBUG, logging.INFO):
        out = fmt.format(_record(level))
        # level name is still colored ...
        assert ESC in out
        # ... and the color is properly reset right after the level name
        assert log.LEVEL_COLORS[level] + logging.getLevelName(level) + log.RESET in out


def test_colored_formatter_use_color_false_is_plain():
    fmt = log.ColoredFormatter("%(asctime)s %(levelname)s %(message)s", use_color=False)
    out = fmt.format(_record(logging.ERROR, "boom"))
    assert ESC not in out
    assert "ERROR" in out and "boom" in out


def test_file_formatter_has_no_ansi():
    # The file handler uses a plain logging.Formatter; assert it never colors.
    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s"
    )
    for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
        out = file_fmt.format(_record(level, "Status: x"))
        assert ESC not in out


def test_color_enabled_respects_no_color_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert log.color_enabled(True) is True
    assert log.color_enabled(False) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert log.color_enabled(True) is False


def test_log_factory_file_stays_ansi_free(tmp_path):
    """End-to-end: stdout gets color, the rotating file does NOT."""
    logfile = tmp_path / "t.log"
    app_log = log.log(str(logfile), "DEBUG", True, True)
    try:
        app_log.warning("Status: factory check")
        app_log.error("factory boom")
        for h in app_log.handlers:
            h.flush()
        contents = logfile.read_text(encoding="utf-8")
        assert "Status: factory check" in contents
        assert "factory boom" in contents
        assert ESC not in contents, "file log must be ANSI-free"
        # The stdout handler must be the colored one.
        stream_handlers = [
            h for h in app_log.handlers if isinstance(h, logging.StreamHandler)
            and not hasattr(h, "baseFilename")
        ]
        assert stream_handlers, "expected a stdout StreamHandler"
        assert any(isinstance(h.formatter, log.ColoredFormatter) for h in stream_handlers)
    finally:
        # Don't leak handlers onto the shared 'root' logger for other tests.
        for h in list(app_log.handlers):
            app_log.removeHandler(h)
            h.close()
