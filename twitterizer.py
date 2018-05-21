import json

tweet_id = "998532029049393152"
file = open('secret.json','r').read()
parsed = json.loads(file)

consumer_key=parsed['consumer_key']
consumer_secret=parsed['consumer_secret']
access_token=parsed['access_token']
access_token_secret=parsed['access_token_secret']

import tweepy

auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
auth.set_access_token(access_token, access_token_secret)

api = tweepy.API(auth)

"""
public_tweets = api.home_timeline()
for tweet in public_tweets:
    print (tweet.text)


search_tweets = api.search("$BIS #Bismuth")
for tweet in search_tweets:
    print (tweet.text)
"""

open_status = api.get_status(tweet_id)
parsed = open_status._json
favorite_count = parsed ['favorite_count']
retweet_count = parsed ['retweet_count']
parsed_text = parsed['text']


if "#bismuth" and "$bis" in parsed_text.lower() and retweet_count + favorite_count > 10:
    qualifies = True
else:
    qualifies = False

print (qualifies)


#print (json.loads(str(open_status)))

