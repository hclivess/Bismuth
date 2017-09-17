import time, random

def commit(cursor, app_log):
    """Secure commit for slow nodes"""
    while True:
        try:
            cursor.commit()
            break
        except Exception as e:
            app_log.warning("Retrying database execute due to {} in {}".format(e, cursor))
            time.sleep(random.random())


def execute(cursor, query, app_log):
    """Secure execute for slow nodes"""
    while True:
        try:
            cursor.execute(query)
            break
        except Exception as e:
            app_log.warning("Database query: {} {}".format(cursor, query))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.random())
    return cursor


def execute_param(cursor, query, param, app_log):
    """Secure execute w/ param for slow nodes"""

    while True:
        try:
            cursor.execute(query, param, app_log)
            break
        except Exception as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.random())
    return cursor