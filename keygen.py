#lib for manual key generation

import essentials
import log

if __name__ == "__main__":
    app_log = log.log ("keygen.log", "WARNING", True)
    essentials.keys_check (app_log)
    key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, myaddress = essentials.keys_load ("privkey.der", "pubkey.der")
    print("Address:",myaddress)