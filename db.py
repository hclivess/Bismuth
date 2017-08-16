import time

def execute(cursor, query):
    """Secure execute for slow nodes"""
    while True:
        try:
            cursor.execute(query)
            break
        except Exception as e:
            print("Retrying database execute due to {}".format(e))
            time.sleep(0.1)
            pass # As suggested in another PR, I suggest removing this
    return cursor


def execute_param(cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except Exception as e:
            print("Retrying database execute due to " + str(e))
            time.sleep(0.1)
            pass # As mentioned above, I suggest removing this
    return cursor
