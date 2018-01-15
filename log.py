import logging, sys
from logging.handlers import RotatingFileHandler

def log(logFile,level):
    if level == "INFO":
        level = logging.INFO
    if level == "DEBUG":
        level = logging.DEBUG
    if level == "WARNING":
        level = logging.WARNING

    log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')
    my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5 * 1024 * 1024, backupCount=2, encoding=None, delay=0)
    my_handler.setFormatter(log_formatter)
    my_handler.setLevel(level)
    app_log = logging.getLogger('root')
    app_log.setLevel(level)
    app_log.addHandler(my_handler)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    formatter = logging.Formatter('%(asctime)s %(funcName)s(%(lineno)d) %(message)s')
    ch.setFormatter(formatter)
    app_log.addHandler(ch)

    return app_log