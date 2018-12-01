import sqlite3
import time

conn = sqlite3.connect("../static/ledger.db")
conn.text_factory = str
c = conn.cursor()

block = 900000
health = 100
strength = 10
alive = True

#trigger is followed by events affected by modifiers

#define events
events = {"9c" : "heal",
          "6c" : "attack",
          "e2" : "attacked",
          "8f" : "critical_hit"}

#define modifiers
modifiers = {"a1c" : "health_belt",
             "082" : "enchanted_sword"}

#define triggers
triggers = {"4f" : "troll",
            "df" : "goblin",
            "5a" : "berserk",
            "61a" : "dragon"
            }

def cycle(block):
    c.execute("SELECT * FROM transactions WHERE block_height = ? ORDER BY block_height", (block,))
    result = c.fetchall()
    block_hash  = result[0][7]
    return block_hash

while alive:

    block_hash = cycle(block)

    for trigger_key in triggers:
        if trigger_key in block_hash:
            print("You meet {}".format(triggers[trigger_key]))


            while alive:
                block_hash = cycle(block)

                for event_key in events:
                    if event_key in block_hash:
                        print("Event: {}".format(events[event_key]))

                        if events[event_key] == "attack":
                            print ("{} killed! You stride forward with {} HP...".format(triggers[trigger_key], health))
                            break

                        if events[event_key] == "heal":
                            if health < 100:
                                health = health + 20
                                print ("You rest and heal to {} HP...".format(health))
                            if health > 100:
                                health = 100
                            break

                        if events[event_key] == "attacked":
                            health = health - 60
                            print("{} hit you for 60 HP, you now have {} HP".format(triggers[trigger_key], health))
                            if health < 1:
                                alive = False

                block = block + 1
                time.sleep(2)


    block = block+1



