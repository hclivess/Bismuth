from Crypto.PublicKey import RSA
import hashlib


def recover(key):
    private_key_readable = key.exportKey().decode("utf-8")
    public_key_readable = key.publickey().exportKey().decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
    print ("Address: {}".format(address))

    recovery_file_priv = "privkey_recovered.der"
    recovery_priv = open(recovery_file_priv, 'w')
    recovery_priv.write(str(private_key_readable))
    recovery_priv.close()
    print ("Private key recovered to: {}".format(recovery_file_priv))

    recovery_file_pub = "pubkey_recovered.der"
    recovery_pub = open(recovery_file_pub, 'w')
    recovery_pub.write(str(public_key_readable))
    recovery_pub.close()
    print ("Public key recovered to: {}".format(recovery_file_pub))

    return (address, recovery_file_priv, recovery_file_pub)





