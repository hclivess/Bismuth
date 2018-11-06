#make sure no operations are possible before birth

import sqlite3, time, random, sys

address = "fefb575972cd8fdb086e2300b51f727bb0cbfc33282f1542e19a8f1d"
conn = sqlite3.connect("static/ledger.db")
conn.text_factory = str
c = conn.cursor()

c.execute("SELECT openfield FROM transactions WHERE address = ? AND operation = ?", (address,"petbirth"))
result = c.fetchall()

def name(entry):
    pet_name = entry[0]
    return pet_name

def age(name):
    c.execute("SELECT timestamp FROM transactions WHERE address = ? AND operation = ? AND openfield = ?", (address, "petbirth", name))
    birth_time = c.fetchone()[0]
    #print (birth_time)

    now = time.time()
    age = int((now - birth_time) / 60)
    return age

def died_between_feeding():
    pass

def died_of_age(name, age):
    pet_died_of_age = True if age > 20 else False
    return pet_died_of_age
    #add checks between feedings


def fed(name):
    c.execute("SELECT timestamp FROM transactions WHERE address = ? AND operation = ? AND openfield = ?", (address, "petfeed", name))

    fed_last = c.fetchone()
    fed_last = fed_last[0] if fed_last is not None else fed_last

    now = time.time()
    
    if fed_last:
        pet_fed = int((now - fed_last) / 60)
    else:
        pet_fed = False

    return pet_fed


for entry in result:
    #print (entry)

    pet_name = (name(entry))
    print ("name",pet_name)

    pet_age = (age(pet_name))
    print ("age",pet_age)

    pet_died_of_age = (died_of_age(pet_name,pet_age))
    print ("died",pet_died_of_age)

    pet_fed = (fed(pet_name))
    print ("fed",pet_fed)


