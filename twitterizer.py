#add additional info to the tweet: address that is allowed to withdraw
sleep_interval = 900
payout_level = 1
payout_gap = 4
month = 2629743
lookback = 20

import tweepy
import json
import options
import os
import sqlite3
import essentials
import connections
import socks
import time

config = options.Get()
config.read()
debug_level = config.debug_level_conf
ledger_path_conf = config.ledger_path_conf
full_ledger = config.full_ledger_conf
ledger_path = config.ledger_path_conf
hyper_path = config.hyper_path_conf
terminal_output=config.terminal_output
version = config.version_conf

file = open ('secret.json', 'r').read ()
parsed = json.loads (file)
consumer_key = parsed['consumer_key']
consumer_secret = parsed['consumer_secret']
access_token = parsed['access_token']
access_token_secret = parsed['access_token_secret']
auth = tweepy.OAuthHandler (consumer_key, consumer_secret)
auth.set_access_token (access_token, access_token_secret)
api = tweepy.API (auth)

key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, myaddress = essentials.keys_load()

def tweet_saved(tweet):
    try:
        t.execute("SELECT * FROM tweets WHERE tweet = ?", (tweet,))
        dummy = t.fetchone()[0]
        return_value = True
    except:
        return_value = False

    print(return_value)
    return return_value

def tweet_qualify(tweet_id, exposure=10):
    try:
        open_status = api.get_status(tweet_id)
        parsed = open_status._json
        #print(parsed)
        #time.sleep(900)

        parsed_id = parsed ['user']['id'] #add this
        favorite_count = parsed ['favorite_count']
        retweet_count = parsed ['retweet_count']
        parsed_text = parsed['text']
        parsed_followers = parsed['user']['followers_count']
        acc_age = time.mktime (time.strptime (parsed['user']['created_at'], '%a %b %d %H:%M:%S +0000 %Y'))

        if "#bismuth" and "$bis" in parsed_text.lower() and retweet_count + favorite_count > exposure and parsed_followers > 30 and acc_age < time.time() - month:
            qualifies = True
        else:
            qualifies = False

        print (parsed_id, favorite_count, retweet_count, parsed_text, parsed_followers, acc_age, qualifies)

    except Exception as e:
        print ("Exception with {}: {}".format(tweet_id,e))
        qualifies, parsed_text, parsed_id = False,False,False

    return qualifies, parsed_text, parsed_id


if __name__ == "__main__":
    if not os.path.exists ('twitter.db'):
        # create empty mempool
        twitter = sqlite3.connect ('twitter.db')
        twitter.text_factory = str
        t = twitter.cursor()
        t.execute ("CREATE TABLE IF NOT EXISTS tweets (block_height, address, openfield, tweet, user)")
        twitter.commit ()
        print ("Created twitter database")
    else:
        twitter = sqlite3.connect ('twitter.db')
        twitter.text_factory = str
        t = twitter.cursor()
        print ("Connected twitter database")

    #ledger
    if full_ledger == 1:
        conn = sqlite3.connect (ledger_path)
    else:
        conn = sqlite3.connect (hyper_path)
    if "testnet" in version:  # overwrite for testnet
        conn = sqlite3.connect ("static/test.db")

    conn.text_factory = str
    c = conn.cursor()
    # ledger


    while True:
        #twitter limits: 180 requests per 15m
        for row in c.execute ("SELECT * FROM (SELECT block_height, address, openfield FROM transactions WHERE operation = ? ORDER BY block_height DESC LIMIT ?) ORDER BY block_height ASC", ("twitter",lookback)): #select top *, but order them ascendingly so older have priority

            tweet_id = row[2]
            tweet_qualified = tweet_qualify (tweet_id)
            name = tweet_qualified[2]

            t.execute("SELECT COUNT() FROM (SELECT * FROM tweets ORDER BY block_height DESC LIMIT ?) WHERE name = ?",(payout_gap, name,))
            name_count = t.fetchone()[0]


            if tweet_qualified[0] and not tweet_saved(tweet_qualified[1]) and name_count < 1:
                print ("Tweet qualifies")

                recipient = row[1]
                amount = payout_level
                operation = "payout_tw"
                openfield = ""

                timestamp = '%.2f' % time.time ()
                tx_submit = essentials.sign_rsa(timestamp, myaddress, recipient, amount, operation, openfield, key, public_key_hashed)

                if tx_submit:
                    s = socks.socksocket ()
                    s.settimeout (0.3)
                    print(tx_submit)

                    s.connect (("127.0.0.1", int (5658)))
                    print ("Status: Connected to node")
                    while True:
                        connections.send (s, "mpinsert", 10)
                        connections.send (s, tx_submit, 10)
                        reply = connections.receive (s, 10)
                        print ("Payout result: {}".format (reply))
                        break

                    if reply[-1] == "Success":
                        t.execute ("INSERT INTO tweets VALUES (?, ?, ?, ?, ?)", (row[0], row[1], row[2], tweet_qualified[1], name))
                        twitter.commit ()
                        print ("Tweet saved to database")
                        api.retweet(tweet_id)
                        api.update_status ("Bismuth address {} wins giveaway of {} $BIS for https://twitter.com/i/web/status/{}".format(recipient, amount,tweet_id))
                    else:
                        print("Mempool insert failure")

                break


        print ("Run finished, sleeping for {} minutes".format(sleep_interval/60))
        time.sleep(sleep_interval)
