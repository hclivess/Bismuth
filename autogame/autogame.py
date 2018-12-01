import sqlite3
import time

conn = sqlite3.connect("../static/ledger.db")
conn.text_factory = str
c = conn.cursor()

block = 900000

class Hero:
    def __init__(self):
        self.health = 100
        self.strength = 10
        self.alive = True
        self.in_combat = False

hero = Hero()

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

while hero.alive:

    block_hash = cycle(block)

    for trigger_key in triggers:
        if trigger_key in block_hash:
            print("You meet {}".format(triggers[trigger_key]))


            while hero.alive:
                block_hash = cycle(block)

                for event_key in events:
                    if event_key in block_hash:
                        print("Event: {}".format(events[event_key]))

                        if events[event_key] == "attack":
                            print ("{} killed! You stride forward with {} HP...".format(triggers[trigger_key], hero.health))
                            break

                        if events[event_key] == "heal" and not hero.in_combat:
                            if hero.health < 100:
                                hero.health = hero.health + 20
                                print ("You rest and heal to {} HP...".format(hero.health))
                            if hero.health > 100:
                                hero.health = 100
                            break

                        if events[event_key] == "attacked":
                            hero.health = hero.health - 60
                            print("{} hit you for 60 HP, you now have {} HP".format(triggers[trigger_key], hero.health))
                            if hero.health < 1:
                                hero.alive = False

                block = block + 1
                time.sleep(2)


    block = block+1



