---
title: Bismuth — the first Python Blockchain
author: Blackbeard
source: Medium
date: Jan 16, 2019
original_url: https://medium.com/@cblackbeard/bismuth-the-first-python-blockchain-6e2fb0b53a4f
---

An Interview with Jan — Founder and Lead Developer

**Good day Jan, everyone has his own story on how he/she got into blockchain technology. What's yours?**

I have been coding since I was 8 but I only knew some basics back then. After I graduate I have spent exactly 5 years working in the banking, insurance, and retail industry in positions of application performance analyst and end user experience analyst, that's where I learned Python. I have moved to crypto space since the 2013 bubble.

**What is your role at Bismuth?**

I am the founder and leading dev of the Bismuth blockchain and I started working on building Bismuth from scratch back in 2015.

**Who is behind Bismuth? What are the Backgrounds of the team member?**

Behind Bismuth, there is a team of 9 members and 3 advisors. Because some of the team members wanted to remain anonymous, we respect their decision and we will not discuss their background.

**What is Bismuth and how would you explain it to non-crypto speakers?**

Bismuth is trying to be modular, extensible and as simple as possible. To make an analogy, let's say Bismuth is blockchain Lego. If you want to work with it, you can take inspiration from the many operational open source projects and use them as a reference to develop your own.

**How did the idea to develop Bismuth come about?**

I wanted to help with other projects originally but found out that it would take a considerable time to study the BTC codebase and understand all dependencies. Therefore, I went to create what was supposed to be the smallest possible viable blockchain code written from scratch, from which Bismuth developed.

**Why did you choose Python to develop the blockchain?**

You don't need to compile the code after every tiny change you make, which saves hours of work. Python has multiple standard libraries and many other available, so it makes code easier to write, read and extend very quickly. It is a language with ideal performance/simplicity ratio for block time-based systems and many-participant systems that are limited by network performance in general.

**What are the main use cases you are looking to cover?**

Things that would need a dedicated app or chain (on chain document fingerprinting, encrypted messaging, tokens…) are native in Bismuth. But since Bismuth is more a Lego than a specific end-user app, it does not target a specific vertical or use case but allows developers to easily build upon it and target what they need. Games are the first use case that emerged from the core and third-party developers.

Bismuth structure is very flexible, with on- and off chain protocols that already are used. There is a demo of Event sourcing, for instance, that can be used for things like a twitter app or a secure, auditable database. Secure and paid document archiving is another thing that is being worked on. Side chains, already running with the Hypernodes, showcase the use of Bismuth as a master Pow chain to secure others.

The philosophy of Bismuth is to be as close to the real world as possible: do not try to build complex algorithms that are mathematically perfect, ignoring the interface with the real world. Bismuth acknowledges the real world usage before all. Allowing arbitrary data storage as part of the transactions goes in that direction.

**What makes Bismuth stand out when it comes to blockchain and dApps, how did you achieve this and how is the competition doing it?**

I believe that Bismuth is the first platform written purely in Python for modularity and we offer an arbitrary on-chain data system, so anyone can really create anything.

We also have taken the philosophy of the dApps engines to prevent on-chain bloat like on Ethereum, so the state of decentralized operations is not shared across all nodes, but only by those who decide to interact with particular apps.

Some other projects will enable changing the rules by voting like Tezos, while Bismuth does not force you into any decisions enforced by any group, you can simply choose whether you want to participate in this or that module.

We have a very pragmatic approach. We make it work first, then we make it better, with real-world experiences help.

But the main thing about Bismuth is its modularity, flexibility, and openness. It's thought to be tweakable, and I don't think any other crypto goes that far:

- Like we saw, the core protocol allows arbitrary data, and supports abstract transactions, therefore smart protocols also.

- The node itself supports plugins, written in python. Like with WordPress, you can add a small file with a few lines of python code, and you've added custom features to the node…

- The upcoming wallet also goes in that direction: it supports add-ons so a dApp creator can embed his app or part of it directly in the wallet.

- The client library not only supports Python but also C#, C++, Go, Javascript, PHP! Makes it easy to interface from any app.

**What is the difference between a Hypernode and a Masternode?**

Masternodes are usually very tightly merged into regular nodes. In fact, it's just a few more functions inside a regular node, to handle the POS staking and signing POW blocks. So usual Masternodes are deeply embedded into the POW layer and operate on the POW blockchain.

Bismuth Hypernodes are completely different peers. They run on their own network, have their own child chain — with different rules than the POW chain — and are more loosely coupled than regular Masternodes. They are not a direct part of the POW chain, do not sign POW blocks. Instead, they are a layer above, that watches over the POW chain and POW actors, measures, audits, reports and store metrics in its own chain.

They are the first running example of Bismuth side chains and give something every blockchain should have: an independent and safe measure tool of the main chain metrics and actor behavior.

**If I take a look on the current Projects on your website, I see a lot of projects listed there, in what time period will these be completed?**

Since the beginning, we didn't shoot out terms regarding the completion of any of our projects because we don't want to promise something that we can't make it happen in time or to set out goals that we can't accomplish. This way you risk to disappoint the investors and the community, so we better under-promise and over-deliver. Moreover, it's often faster to just go and code the thing rather than write that you may do it next week.

**Thinking about the future, do you have any roadmap with relevant upgrades or partnerships?**

What's next on our roadmap? I would personally like to see full-blown smart contracts, generalize the Hypernode technology into a side/child chain secure framework, make a real-world use case of the Hyperlane Technology and cold staking for Fuel. As for the projects, our goals are safe document archiving, embedded hardware devices and WordPress open source e-commerce plugin for Bismuth. In terms of partnerships, we welcome third party devs to work on dApps. We offer our help and support to anyone coming with a viable idea to implement on top of Bis.

**Any plans to get listed on other exchanges?**

Speaking of exchanges, first I want to let you know that we are listed on 3 exchanges, TradeSatoshi.com, and qTrade.io but since there is a low or non-existent volume in there, only Cryptopia is listed on CoinMarketCap at Bismuth markets section. Of course, we are in contact with several top exchanges and we are working to add Bismuth on more exchanges in the future, but at the end of the day is their final call about the listing, not ours.

**How many funds do the core team have in the budget for development and marketing?**

This information is available on bismuth.online explorer under Rich List tab, the "Test and Development Fund" is our fund and is holding about 46 BTC or $165000 at the time of writing.

Thank you for your time. If you enjoyed the read feel free to follow me on Twitter.

Blackbeard
