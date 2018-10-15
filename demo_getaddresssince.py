"""

Demo script for the api_getaddresssince api command.

takes
- last known block
- min confirmations
- address

Sends back at most 720 blocks worth of data with

- last considered block
- min confirmations
- list of tx matching the address

Usage:

python3 demo_getaddresssince.py block_height min_conf address

eg:
`python3 demo_getaddresssince.py 864115 20 edf2d63cdf0b6275ead22c9e6d66aa8ea31dc0ccb367fad2e7c08a25`


No proper error checking.
"""

import connections
import json
import socks
import sys


__version__ = "0.0.1"


def get_address_since(since, min_conf, address):
    s = socks.socksocket()
    s.settimeout(10)
    s.connect(("127.0.0.1", 5658))
    # Command first
    connections.send(s, "api_getaddresssince")
    # Then last block (will not be included in results
    connections.send(s, int(since))
    # min confirmations
    connections.send(s, int(min_conf))
    # and finally the address
    connections.send(s, str(address))

    res = connections.receive(s)
    return res


if __name__ == "__main__":
    _, since, min_conf, address = sys.argv
    print("api_getaddresssince since {} minconf={} for address {}".format(since, min_conf, address))
    res_as_native_dict = get_address_since(since, min_conf, address)
    print(json.dumps(res_as_native_dict))
