import threading
import asyncio

class HyperlaneManager (threading.Thread):
    def __init__(self,app_log):
        threading.Thread.__init__(self,target=self.loop_in_thread, args=(self.loop,))
        self.app_log = app_log

    async def hyperlane_manager(self):
        self.app_log.warning("Hyperlane manager initiated")
        while True:
            self.app_log.warning("Hyperlane manager running")
            await asyncio.sleep(5)

    def loop_in_thread(self,loop):
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.hyperlane_manager())

    loop = asyncio.get_event_loop()

if __name__ == "__main__":
    import options
    import log

    config = options.Get()
    config.read()
    app_log = log.log("hyperlane.log", "WARNING", True)

    #wm = HyperlaneManager(app_log)
    #hyperlane_thread = threading.Thread(target=wm.loop_in_thread, args=(wm.loop,))
    #hyperlane_manager = HyperlaneManager(app_log)
    #hyperlane_manager.start()
    hyperlane_manager = HyperlaneManager(app_log)
    hyperlane_manager.start()

    print("we can continue without being blocked")