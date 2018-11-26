Bismuth maximum supply after hypernodes-hardfork
=======

When hypernodes were introduced, the team decided to reduce the maximum supply. When Bismuth-Mainnet started, the formula for rewards was this:
```python
if db_block_height <= 10000000:
    mining_reward = 15 - (quantize_eight(block_height_new) / quantize_eight(1000000))  # one zero less
else:
    mining_reward = 0
```

So, the maximum coins, given out to miners would have been **99999990 BIS**.

Additionally, there is a dev reward, used for feeding the devlopers, but also pay for external developments, feasible exchange listing fees and so on. This reward is payed every 10th block and the amount is the mining_reward of this block.
To make it "easier" to understand, here is the line of code:
```python
if int(block_height_new) % 10 == 0:
    if transaction == block_transactions[-1]:  # put at the end
        execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("0", str(time_now), "Development Reward", str(genesis_conf), str(mining_reward), "0", "0", "0", "0", "0", "0", str(block_height_new)))
    commit(conn)
```
str(mining_reward) is the dev-reward and it is the same as the reward for the actual block.

Adding this up, the maximum supply of Bismuth was:
**109999980 BIS**


## Past Hardfork on block 854660

Ok, now we the hn-hardfork. Here is the actual code for the mining_reward:
```python
if db_block_height <= 10000000:
    mining_reward = 15 - (quantize_eight(block_height_new) / quantize_eight(1000000 / 2)) - Decimal("0.8")
    if mining_reward < 0:
        mining_reward = 0
else:
    mining_reward = 0
```

This is what changed there: **/ 2)) - Decimal("0.8")**

What does this mean?

The reduction per Block doubled. Also 0.8 BIS per block are collected as hypernode rewards. They are paid weekly and they are not part of the reduction.
The hardfork for hypernodes happened at block **854660**. So, until the hardfork, there were around 13727812 BIS (with dev-rewards).

Now adding up with the new formula:

Pre fork: **13727812 BIS**

Post fork: **50195106 BIS**

Therefore, the new max supply is:

Sum: **63922918 BIS**

The actual formula gives zero reward from **block 7100001** and above.


### There is a bigLITTLE thing now at the end:

The hn rewards are actually paid infinitely. Means, after 10.000.000 blocks, the miners rewards are set to 0 (and so are dev rewards), only fees are paid. With the changed formula, the reward for miners and dev is 0 way before the code sets it to 0. Actually this will happen on block 7100000. On every 10th block, the hn rewards are still paid to the actual hn payout address (8 BIS). This is not meant to be the final decision, in fact it was really never discussed, how we handle hypernode rewards after block 10.000.000. You can expect a change here in the future, but as the actual code has an infinite supply, here now some plots to show what it means:

![Oups, where is the plot?](/graphics/rewards.png)

The plot is created by this [script](supply_calc.py) and therefore shows the actual mining reward of the actual block (it gets it by asking [bismuth.online API](http://bismuth.online/api/stats/latestblock)). From this block and time mined, the script calculates the estimated time, when for example block 10.000.000 is mined.

As we can see here, the reward will last for about ... a long time. 


### What does this now mean for the supply?

Few lines above, we mentioned a total supply of approx 64 Mio BIS. This is the supply, we will see on block 10.000.000. **From there on, the supply will rise by 8 Mio BIS every 10 Mio Blocks. A rough estimation is: 420k BIS/year**.

Here another plot from the script with the BIS-supply and how it is spread over the different gainers (miners, hn, dev)

![Oups, where is the plot?](/graphics/supply.png)

So, around summer 2112 (arr, another rough estimation), the BIS-supply will be at around **96 Mio BIS**.
100 Mio BIS will eventually be produced in more than 100 years.


## Conclusion

Yes, we have unlimited supply. 

Yes, we already had discussed this and we think, we still have some years, where BIS will grow (in code and community) until we have to make a decission.

**But as a holder, you told us other numbers!**

Yes, we did. We apologize for that and as said, since that, we have spoken a lot about supply. 

To make it clear, we came down from **110 Mio BIS in 203x**, we actually go the road to **100 Mio BIS in 212x**. 
But this is not the final word on this topic. We already have ideas how to go on, but no decission yet. We are not in a rush to decide it and the last time, we overall did not well, calculating and double checking everything.

Here a table for quick overview:

| Block | Supply | Milestone |
| ------------- | ------------- | ------------- |
| 7100000  | 61602918  | POW-rewards end approx 2030 | 
| 10000000  | 63922918  | Mined approx 2036  |
| 20000000  | 71922918  | Mined approx 2055  |
| 30000000  | 79922918  | Mined approx 2074  |
| 40000000  | 87922918  | Mined approx 2093  |
| 50000000  | 95922918  | Mined approx 2112  |