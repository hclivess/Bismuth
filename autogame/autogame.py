import sqlite3
import time

conn = sqlite3.connect("../static/ledger.db")
conn.text_factory = str
c = conn.cursor()

block = 910000

class Hero:
    def __init__(self):
        self.health = 100
        self.power = 10
        self.alive = True
        self.in_combat = False
        self.experience = 0

hero = Hero()

#trigger is followed by events affected by modifiers

#define events
events = {"9a" : "heal",
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

        if trigger_key in block_hash and hero.alive:
            print("You meet {}".format(triggers[trigger_key]))


            hero.in_combat = True
            while hero.alive and hero.in_combat:
                block_hash = cycle(block)

                for event_key in events:
                    if event_key in block_hash and hero.alive:
                        print("Event: {}".format(events[event_key]))

                        if events[event_key] == "attack":
                            print ("{} killed! You stride forward with {} HP...".format(triggers[trigger_key], hero.health))
                            hero.experience += 1
                            hero.in_combat = False
                            break

                        if events[event_key] == "heal":
                            if hero.in_combat and hero.health < 100:

                                if hero.in_combat:
                                    hero.health = hero.health + 5
                                    print("You drink a potion and heal to {} HP...".format(hero.health))

                                else:
                                    hero.health = 100
                                    print ("You rest and fully heal...")

                                if hero.health > 100:
                                    hero.health = 100

                            break

                        if events[event_key] == "attacked":
                            hero.in_combat = True
                            hero.health = hero.health - 20
                            print("{} hit you for 20 HP, you now have {} HP".format(triggers[trigger_key], hero.health))
                            if hero.health < 1:
                                print("You died with {} experience".format(hero.experience))
                                hero.alive = False

                block += 1
                time.sleep(2)


    block += 1



