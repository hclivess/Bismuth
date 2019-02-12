import socks, connections, time, sys, json
import options
config = options.Get()
config.read()
version = config.version_conf

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