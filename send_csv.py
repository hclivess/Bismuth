import time

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
args = parser.parse_args()

total = 0
nb = 0
for line in open('rewards.csv' , 'r'):
    data = line.strip().split(',')
    print (data)
    if len(data) > 1:
        try:
            total += float(data[1])
            data[1] = float(data[1]) - 0.01
            command = "{} {} {} {} tx ".format(PYTHON_EXECUTABLE, SEND_PATH, data[1], data[0])
            if args.yes:
                print("Running: {}".format(command))
                os.system(command)
            else:
                print("Check: {}, didn't you forget the magic word?".format(command))
            nb += 1
            time.sleep(1)
        except Exception as e:
            print (e)

print("{} Transactions, {} $BIS total.".format(nb, total))
