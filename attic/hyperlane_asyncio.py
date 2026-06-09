"""Retired asyncio "hyperlane" manager prototype (attic).

An early experiment at running a background asyncio task inside its own thread
to drive a future "hyperlane" subsystem. As shipped it only logs a heartbeat
every few seconds; it is not wired into the node. Kept for reference under
attic/ and runnable standalone via ``python3 hyperlane_asyncio.py``.
"""

import threading
import asyncio

class HyperlaneManager (threading.Thread):
    def __init__(self, app_log):
        # Create a dedicated event loop per instance. (Previously a class-level
        # asyncio.get_event_loop() ran at import time, which raises on Python 3.10+.)
        self.loop = asyncio.new_event_loop()
        threading.Thread.__init__(self, target=self.loop_in_thread, args=(self.loop,))
        self.app_log = app_log

    async def hyperlane_manager(self):
        """The long-running coroutine: log a heartbeat forever."""
        self.app_log.warning("Hyperlane manager initiated")
        while True:
            self.app_log.warning("Hyperlane manager running")
            await asyncio.sleep(5)

    def loop_in_thread(self, loop):
        """Thread body: bind the event loop to this thread and run the coroutine."""
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.hyperlane_manager())

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