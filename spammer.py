#this script is based off send.py and is used to spam the network with human-like transactions

import hashlib
import sqlite3
import socket
import time
import base64
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import random

# import keys
key = RSA.importKey(open('privkey.der').read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(public_key_readable).hexdigest()

while True:
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  #s.settimeout(1)
  s.connect(("127.0.0.1", int("2829")))
  print "Connected"

  to_address_list = ["0c9660cb4dd9cceb1732b64c0cbbf664bc1c085f579e8b3c8a5f818a","c12b77c620e0079591609a3dffe61cad3d8b9e2dc1a5cc1534595ddd","cedd2d7ad5c38015cd020228ad2df7d972a7e9587accef76804437a8","e31fd787e1c547996fb3059c4478a4364c8079df6e49a5616c870394","07fb3a0e702f0eec167f1fd7ad094dcb8bdd398c91999d59e4dcb475","f4b18de3135491127e36f42ada93aa232734df92948ef66446423782","6626da8c67863bd65cbf96cfcd668aee82b1b5164578ff4d01520e1d","f33582a7a8374694e80b2708e22b23b6183e0a94e867d43007d44477","060cf08690b41d0ec0cc4506e565dc8fb3ef3ea3807dd19b8003813d","c5ada98e216c6b6525f82991d9b64daed394d9b40de69527662c1e08","c86d133aa40655d1994cfcb3afc48535d917cbf3e99d94eb8e10dad5","7be4f0d6facd2d6125b2a32abc1a261f40e9455c78b0ac3a7e991dd9","0842a5a3a3e2c685333480629644c74d503142d63471a9dec3323685","460bbb99bc66cae942e33c5a92064995c93cc6a0b4bfc17442516ed3","c4b0121f213469764ac653ac29f670448fefef8e5cddbe164c534922","4be74c848a16316e9c8dabd390996588af6ca027547ffecb4495aeee"]
  to_address = random.choice(to_address_list)
  amount =  random.uniform (0.01, 0.1)
  print to_address
  print amount  
      
  timestamp = str(time.time())

  transaction = str(timestamp) + ":" + str(address) + ":" + str(to_address) + ":" + str(float(amount))
  print transaction

  h = SHA.new(transaction)
  signer = PKCS1_v1_5.new(key)
  signature = signer.sign(h)
  signature_enc = base64.b64encode(signature)
  print("Client: Encoded Signature: "+str(signature_enc))

  print("Client: The signature is valid, proceeding to send transaction, signature, new txhash and the public key")
  s.sendall("transaction")
  time.sleep(0.1)
  transaction_send = (transaction+";"+str(signature_enc)+";"+public_key_readable)

  #announce length
  txhash_len = len(str(transaction_send))
  while len(str(txhash_len)) != 10:
      txhash_len = "0" + str(txhash_len)
  print("Announcing " + str(txhash_len) + " length of transaction")
  s.sendall(str(txhash_len))
  time.sleep(0.1)
  # announce length

  s.sendall(transaction_send)
  time.sleep(0.1)

  s.close()
  time.sleep (300)