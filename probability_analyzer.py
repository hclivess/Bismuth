#casino probabilitz calculator

import sqlite3
import re

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

zeroes = 0
ones = 0
twos = 0
threes = 0
fours = 0
fives = 0
sixes = 0
sevens = 0
eights = 0
nines = 0



for row in c.execute("SELECT block_hash FROM transactions WHERE reward != 0"):
    print ("row:",row[0])
    digit_last = (re.findall("(\d)", row[0]))[-1]
    if digit_last == "0":
        zeroes = zeroes + 1
    if digit_last == "1":
        ones = ones + 1
    if digit_last == "2":
        twos = twos + 1
    if digit_last == "3":
        threes = threes + 1
    if digit_last == "4":
        fours = fours + 1
    if digit_last == "5":
        fives = fives + 1
    if digit_last == "6":
        sixes = sixes + 1
    if digit_last == "7":
        sevens = sevens + 1
    if digit_last == "8":
        eights = eights + 1
    if digit_last == "9":
        nines = nines + 1

print ("zeroes:",zeroes)
print ("ones:",ones)
print ("twos:",twos)
print ("threes:",threes)
print ("fours:",fours)
print ("fives:",fives)
print ("sixes:",sixes)
print ("sevens:",sevens)
print ("eights:",eights)
print ("nines:",nines)



