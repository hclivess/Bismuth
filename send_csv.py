"""Batch reward payout sender driven by a CSV file.

Reads ``rewards.csv`` (one ``address,amount,extra`` payout per line) and, for
each row, shells out to :mod:`send_nogui_noconf` to broadcast the payment via a
running node. Without ``--yes`` it only prints the commands it would run (dry
run); with ``--yes`` it actually sends. See the usage notes below for the exact
CSV format and the (intentional lack of) safety guarantees.
"""

import time
import sys

"""
Call send_nogui.py (edit not to require manual confirmation)
for each address to pay.

rewards.csv is to be in the same dir.
format is one payout per line, comma separated, address,amount,extra

```25125e9bb305fafd51ceb2858d355f77da99550b933ec0923cd156ff,1310.4750655411829,5111
8f2d03c817c3d36a864c99a27f6b6179eb1898a631bc007a7e0ffa39,603.0595488461871,2352
0fc9b60126b8b5be3ab990eea6f184b02c1c0c5352709d023256ca58,459.7303448474547,1793```

Amount really sent will be reduced by the tx fee, 0.01

NO SAFETY there, be sure what you do.

The node has to be running with mempool on disk, not on ram or send_nogui does not work!!!
"""



import argparse
import os

__version__ = "0.0.1"


SEND_PATH = "send_nogui_noconf.py" # path to modified send_no_gui.py in the Bismuth Dir.
# That node has to be running with mempool on disk, not on ram!!!

PYTHON_EXECUTABLE = "python3"

parser = argparse.ArgumentParser(description='Bismuth Batch reward sender')
# parser.add_argument("-v", "--verbose", action="count", default=False, help='Be verbose.')
parser.add_argument("-y", "--yes", action="count", default=False, help='Do send')
parser.add_argument("-w", "--wallet", help='Path to wallet, use quotation marks')
args = parser.parse_args()

if not args.wallet:
    parser.error("--wallet/-w is required (path to wallet.der)")

total = 0
nb = 0
for line in open('rewards.csv' , 'r'):
    data = line.strip().split(',')
    print (data)
    if len(data) > 1:
        try:
            total += float(data[1])
            data[1] = float(data[1]) - 0.01
            # send_nogui_noconf.py positional args: amount recipient operation openfield wallet
            command = f'{PYTHON_EXECUTABLE} {SEND_PATH} {data[1]} {data[0]} "" "" "{args.wallet}"'
            if args.yes:
                print(f"Running: {command} tx")
                os.system(command)
            else:
                print(f"Check: {command}, didn't you forget the magic word?")
                sys.exit(0)
            nb += 1
            time.sleep(1)
        except Exception as e:
            print (e)

print(f"{nb} Transactions, {total} $BIS total.")
