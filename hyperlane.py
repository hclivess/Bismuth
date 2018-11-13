import threading
import time

class HyperlaneManager (threading.Thread):
   def __init__(self,app_log):
       threading.Thread.__init__(self)
       self.app_log = app_log

   def run(self):
       hyperlane_manager(self.app_log)

def hyperlane_manager(app_log):
    app_log.warning("Hyperlane manager initiated")
    while True:
        time.sleep(5)