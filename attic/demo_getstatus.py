"""

Demo script for the getstatusjson command.

Sends back a json with node status including walletversion, blocks and connections

Usage:

python3 demo_getstatus.py

Sample output:
```
{
  "protocolversion": "mainnet0020",
  "address": "private",
  "walletversion": "4.3.0.1",
  "testnet": false,
  "blocks": 1215670,
  "timeoffset": 0,
  "connections": 15,
  "connections_list": {
    "188.165.199.153": 1215670,
    "51.15.90.15": 1215670,
    "91.121.77.179": 1215670,
    "51.15.213.94": 1215670,
    "51.15.118.29": 1215670,
    "51.15.254.16": 1215670,
    "51.15.46.90": 1215670,
    "198.245.62.30": 1215670,
    "163.172.222.163": 1215670,
    "51.15.47.212": 1215670,
    "91.121.87.99": 1215670,
    "46.105.43.213": 1215670,
    "149.28.120.120": 1215670,
    "139.180.199.99": 1215670,
    "127.0.0.1": 1215670
  },
  "difficulty": 105.7334696476,
  "threads": 21,
  "uptime": 190503,
  "consensus": 1215670,
  "consensus_percent": 100.0,
  "server_timestamp": "1560781833.09"
}
```



"""

import connections
import json
import socks
import sys


__version__ = "0.0.1"


def get_status():
    s = socks.socksocket()
    s.settimeout(10)
    s.connect(("127.0.0.1", 5658))
    # Command first
    connections.send(s, "statusjson")  # despite the name, it returns a dict
    res = connections.receive(s)
    return res


if __name__ == "__main__":
    res_as_json = get_status()
    print(json.dumps(res_as_json, indent=2))
