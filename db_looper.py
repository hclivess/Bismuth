import threading
import time

class DbManager (threading.Thread):

    def __init__(self,app_log):
       threading.Thread.__init__(self)
       self.app_log = app_log

    def run(self):
       self.hyperlane_manager()

    def hyperlane_manager(self):
        self.app_log.warning("DbManager initiated")
        while True:
            self.app_log.warning("DbManager running")
            time.sleep(5)