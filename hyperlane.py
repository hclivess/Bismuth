import threading
import time

class HyperlaneManager (threading.Thread):

    def __init__(self,app_log):
       threading.Thread.__init__(self)
       self.app_log = app_log

    def run(self):
       self.hyperlane_manager()

    def hyperlane_manager(self):
        self.app_log.warning("Hyperlane manager initiated")
        while True:
            self.app_log.warning("Hyperlane manager running")
            time.sleep(5)



if __name__ == "__main__":
    import options
    import log

    config = options.Get ()
    config.read ()
    app_log = log.log ("hyperlane.log", "WARNING", True)

    hyperlane_manager = HyperlaneManager(app_log)
    hyperlane_manager.start()

    app_log.warning ("Loop started")