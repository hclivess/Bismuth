blocks_total = 10000000

block = 2 #starting block
cumulative = 0

while block <= blocks_total:
    reward = 15 - (float(block) / float(1000000))
    #print block
    #print reward
    cumulative = cumulative + reward
    #print cumulative,"\n"
    block = block + 1

print cumulative
