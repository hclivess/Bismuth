from tkinter import *
from tkinter import filedialog,messagebox
import hashlib
import json

def keys_save(private_key_readable, public_key_readable, address):
    wallet_dict = {}
    wallet_dict['Private Key'] = private_key_readable
    wallet_dict['Public Key'] = public_key_readable
    wallet_dict['Address'] = address

    with open ("wallet.der", 'w') as wallet_file:
        json.dump (wallet_dict, wallet_file)

messagebox.showinfo("","Welcome to the key recovery tool. Please select private key.")
privkey = open(filedialog.askopenfilename (multiple=False, initialdir="", title="Select private key")).read()

messagebox.showinfo("","Please select public key.")
pubkey = open(filedialog.askopenfilename (multiple=False, initialdir="", title="Select public key")).read()


address = hashlib.sha224 (pubkey.encode ('utf-8')).hexdigest ()


keys_save(privkey, pubkey, address)

messagebox.showinfo("","Operation complete, exported as wallet.der in this folder")

sys.exit(0)