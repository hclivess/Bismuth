"""Send a ``stop`` command to the locally running node.

Standalone CLI helper: connects to the node on localhost (picking the port from
the configured network mode -- mainnet 5658, testnet 2829, regnet 3030),
delivers a ``stop`` command, and retries until the connection succeeds.
"""

import socks, connections, time, sys, json
import options
config = options.Get()
config.read()
version = config.version

s = socks.socksocket()

port = 5658
if "testnet" in version:
    port = 2829
    print("tesnet mode")
elif "regnet" in version:
    is_regnet = True
    print("Regtest mode")
    port = 3030


while True:
    try:
        s.connect(("127.0.0.1", port))

        print("Sending stop command...")
        connections.send(s, "stop")
        print("Stop command delivered.")
        break
    except:
        print("Cannot reach node, retrying...")

s.close()