---
title: Developer Interview - Draggon's Eggs Soccer Cup
author: bitsignal
source: Steemit
date: 8 years ago
original_url: https://steemit.com/cryptocurrency/@bitsignal/developer-interview-draggon-s-eggs-soccer-cup
---

EggdraSyl interviewed the developer of dragginator.com, which is a fun game on top of the #Bismuth blockchain. This chat about the the Draggon's Eggs Soccer Cup is for you players out there to get to know more about this creative and fantastic game which has many players hooked and luckily the permission was given to post this interview.

**EggdraSyl:** Hi @Iyomisc, I'm not a big sports fan, and I'm a little lost with how this cup is handled, matches organized aso. Can you help me figure it out?

lyomisc: sure

**EggdraSyl:** First of all, the pools: how were the players splits among the pools? Is it pure random or is there some rule?

lyomisc: Each pool had 4 players. I used a custom algo to make sure: a/ each pool has a good team, a bad team, and 2 average teams. b/ not two players from the same owner (address) are in the same pool

**EggdraSyl:** Then, how did you decide what is a good or a bad team?

lyomisc: I used a score based on the team characteristic: the sum of the features that play a role in the simulator. The first player of a pool is the strongest one, the last one is the weakest one. But as you can see in the results, there is always some luck factor :-)

**EggdraSyl:** I see! Then how will the brackets be organized? Who is qualified once the pools matches are played?

lyomisc: The first 2 players of each pool are qualified.

**EggdraSyl:** What metric is used there?

lyomisc: The rank is computed from Points, then Scored goals, then Goal difference, and if still they are even, then DNA - random then.

**EggdraSyl:** And the brackets?

lyomisc: There is a special algorithm, that sorts the qualified players depending on their rank, and has the first of the numbers 1 play with the last of the numbers 2, aso. It's complicated but stick with what is done in official sports competitions. I try to follow as close as possible what is done in the real world. Then each winner goes to the next step until the final.

**EggdraSyl:** You do one match per egg per day. Can't you do it faster? Is it computer intensive?

lyomisc: The simulator itself is fast but I have some other things to do, and I prefer to do it like in the real world. So the players need some rest and will not play more than one match a day.

**EggdraSyl:** The pools screen only shows the total stats, not the individuals matches. Will we see more for the rest of the cup?

lyomisc: You will see the number of goals of each team, then - if there is a draw - the number of penalties. for instance 3 and 2 (no penalties) or 3.2 and 3.3 (3 goals each, then 2 penalties vs 3)

**EggdraSyl:** Great! And can we hope for more insight into the matches, how the simulator works?

lyomisc: Maybe, we will see.

**EggdraSyl:** More graphs and stats as the cup goes on?

lyomisc: I will try to update the graphs, yes.

**EggdraSyl:** Thanks a bunch!

mikeomike: Good info thnx :-) How does it work if I already had 10/11 eggs registered and im going to buy now a new one that has been qualified?

lyomisc: You'll get the egg, and if it wins, you'll receive the price

**EggdraSyl:** Nice! 7 days left then. Can we see the up to date graphs, now that half of the eggs have been eliminated?

lyomisc: Here is the graph of the qualified eggs.

Thank you to lyomisc for giving us some insights about the Draggon's Eggs Soccer Cup.

Link
http://dragginator.com/cup.php
