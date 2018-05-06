import logging, sys
from logging.handlers import RotatingFileHandler



def filter_status(record):
    """"
    Only displays log messages about status info
    or ERROR level
    """
    if ("Status:" in str(record.msg)) or (record.levelname == 'ERROR'):
        return 1
    else:
        return 0


def log(logFile, level_input="WARNING", terminal_output=False):
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
    my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5 * 1024 * 1024, backupCount=2, encoding=None, delay=0)
    my_handler.setFormatter(log_formatter)
    my_handler.setLevel(level)
    app_log = logging.getLogger('root')
    app_log.setLevel(level)
    app_log.addHandler(my_handler)

    # This part is what goes on console.
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    # TODO: We could have 2 level in the config, one for screen and one for files.
    print ("Logging level: {} ({})".format(level_input,level))
    if terminal_output != True:
        ch.addFilter(filter_status)
        # No need for complete func and line info here.
        formatter = logging.Formatter('%(asctime)s %(message)s')
    else:
        formatter = logging.Formatter('%(asctime)s %(funcName)s(%(lineno)d) %(message)s')
    ch.setFormatter(formatter)
    app_log.addHandler(ch) 

    return app_log
