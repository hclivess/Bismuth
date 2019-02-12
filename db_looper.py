import threading
import time
import queue

class DbManager (threading.Thread):

    def __init__(self,app_log):
       threading.Thread.__init__(self)
       self.app_log = app_log
       self.q = queue.Queue()

    def run(self):
       self.db_manager()

    def db_manager(self):
        self.app_log.warning("db_manager initiated")
        while True:
            self.app_log.warning("db_manager running")

            self.app_log.warning("getting queue")

            if self.q:
                queue_item = self.q.get()
                self.app_log.warning("sending queue")


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