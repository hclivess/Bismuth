import threading
import time

class DbManager (threading.Thread):

    def __init__(self,app_log):
       threading.Thread.__init__(self)
       self.app_log = app_log

    def run(self):
       self.db_manager()

    def db_manager(self):
        self.app_log.warning("db_manager initiated")
        while True:
            self.app_log.warning("db_manager running")
            time.sleep(5)

if __name__ == "__main__":
    import options
    import log

    config = options.Get()
    config.read()
    app_log = log.log("db_manager.log", "WARNING", True)

    db_manager = DbManager(app_log)
    db_manager.start()

    print("we can continue without being blocked")