import logging, os, sys
from logging.handlers import RotatingFileHandler



# ANSI escape sequences. Kept tasteful: color the level name (and the message for
# WARNING and above so routine "Status:" lines read yellow and errors pop red),
# render the timestamp dim. Everything is reset with RESET so colors never bleed.
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

LEVEL_COLORS = {
    logging.DEBUG: "\033[90m",      # bright black / grey
    logging.INFO: "\033[36m",       # cyan
    logging.WARNING: "\033[33m",    # yellow
    logging.ERROR: "\033[31m",      # red
    logging.CRITICAL: "\033[1;91m", # bold bright red
}


def color_enabled(log_color=True):
    """
    Decide whether ANSI color should be emitted on stdout.

    We deliberately do NOT gate on sys.stdout.isatty(): under systemd the node's
    stdout is a pipe to journald (not a tty), and journalctl renders ANSI when it
    later writes to a terminal. The de-facto NO_COLOR standard still wins so color
    can be disabled without touching the config.
    """
    if os.environ.get("NO_COLOR"):
        return False
    return bool(log_color)


class ColoredFormatter(logging.Formatter):
    """
    Formatter that injects ANSI color BY LEVEL into the rendered line.

    Used ONLY on the stdout/StreamHandler so the rotating log files stay plain.
    The timestamp is dimmed, the level name is colored per LEVEL_COLORS, and for
    WARNING and above the message is colored too so severity is visible at a glance.
    """

    def __init__(self, fmt=None, datefmt=None, use_color=True):
        super().__init__(fmt, datefmt)
        self.use_color = use_color

    def format(self, record):
        if not self.use_color:
            return super().format(record)
        color = LEVEL_COLORS.get(record.levelno, "")
        # Work on a shallow copy of the volatile attributes so we never mutate the
        # shared record (other handlers, e.g. the file handler, format it too).
        original_levelname = record.levelname
        original_asctime = getattr(record, "asctime", None)
        try:
            record.levelname = "{}{}{}".format(color, original_levelname, RESET)
            formatted = super().format(record)
        finally:
            record.levelname = original_levelname
            if original_asctime is None:
                # super().format may have set asctime; drop it so a later plain
                # formatter recomputes it cleanly.
                if hasattr(record, "asctime"):
                    del record.asctime
            else:
                record.asctime = original_asctime
        return formatted

    def formatTime(self, record, datefmt=None):
        # Dim just the timestamp.
        asctime = super().formatTime(record, datefmt)
        if not self.use_color:
            return asctime
        return "{}{}{}".format(DIM, asctime, RESET)

    def formatMessage(self, record):
        # Color the message body for WARNING and above so genuine warnings/errors
        # pop; leave DEBUG/INFO bodies uncolored to keep routine output calm.
        message = super().formatMessage(record)
        if not self.use_color or record.levelno < logging.WARNING:
            return message
        color = LEVEL_COLORS.get(record.levelno, "")
        if not color:
            return message
        # The level name already carries its own color+reset, so re-wrapping the
        # whole line is safe: the trailing RESET clears everything.
        return "{}{}{}".format(color, message, RESET)


def filter_status(record):
    """"
    Only displays log messages about status info
    or ERROR level
    """
    if ("Status:" in str(record.msg)) or (record.levelname == 'ERROR'):
        return 1
    else:
        return 0


def log(logFile, level_input="WARNING", terminal_output=False, log_color=True):
    if level_input == "NOTSET":
        level = logging.NOTSET
    if level_input == "DEBUG":
        level = logging.DEBUG
    if level_input == "INFO":
        level = logging.INFO
    if level_input == "WARNING":
        level = logging.WARNING
    if level_input == "ERROR":
        level = logging.ERROR
    if level_input == "CRITICAL":
        level = logging.CRITICAL

    log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')
    my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8", delay=0)
    my_handler.setFormatter(log_formatter)
    my_handler.setLevel(level)
    app_log = logging.getLogger('root')
    app_log.setLevel(level)
    app_log.addHandler(my_handler)

    # This part is what goes on console.
    use_color = color_enabled(log_color)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    # TODO: We could have 2 level in the config, one for screen and one for files.
    print ("Logging level: {} ({}){}".format(level_input, level, " [color]" if use_color else ""))
    if terminal_output != True:
        ch.addFilter(filter_status)
        # No need for complete func and line info here, but DO surface the level so
        # severity is visible at a glance: "<dim asctime> <COLORED LEVELNAME> <message>".
        formatter = ColoredFormatter('%(asctime)s %(levelname)s %(message)s', use_color=use_color)
    else:
        formatter = ColoredFormatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s', use_color=use_color)
    ch.setFormatter(formatter)
    app_log.addHandler(ch)

    return app_log
