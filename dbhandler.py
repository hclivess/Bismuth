"""
Database handler module for Bismuth nodes
Needed for Json-RPC server or other third party interaction

modular handlers will need access to the database under some form, so it needs to be modular too.
Here, I just duplicated the minimum needed code from node, further refactoring with classes will follow.
"""


import time, random


def execute(app_log, cursor, query):
    """Secure execute for slow nodes"""
    while True:
        try:
            cursor.execute(query)
            break
        except Exception as e:
            app_log.warning("Database query: {} {}".format(cursor, query))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.uniform(0, 1))
    return cursor


def execute_param(app_log, cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except Exception as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.random())
    return cursor
