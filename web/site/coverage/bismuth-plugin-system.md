---
title: The Bismuth plugin system
author: bitsignal
source: Steemit
date: 8 years ago
original_url: https://steemit.com/cryptocurrency/@bitsignal/the-bismuth-plugin-system
---

When I noticed that #Bismuth ($bis) has a plugin system I am somewhat surprised because I do not know any other blockchain based #cryptocurrency which states having incorporated a plugin system.

I feel like that this feature is special knowing how powerful plugins are in conventional blogging and forum applications.

Plugins enhance another software application. This alone sounds intriguing.

The following questions came to mind when checking out Bismuth's plugin system and the developer EggdraSyl & Iyomisc answered them gladly.

**Now, I am wondering how Bismuth achieved the connection between cutting edge blockchain technology and traditional plugin applications?**

Egg: Well, in fact - as often - it came from a personal need. For my pools as well as for notification and monitoring system, I needed more info - and faster - than what the node was able to provide. I first tried other approaches, like a json-rpc server (another layer, verbose, plus latency) or modding the node code (risky, plus a real pain when you need to update the core code) The I proposed a few native api_commands that have been merged in the core, but I needed more :D Then, as I know WordPress pretty well, I thought of the plugins and filter system of WP, that solves those issues: you can write extensions to the core, that are not lost when you upgrade.

As far as I know, no other crypto has that. Bismuth plugins allow to extend the node's functionality, using only few lines of Python. No new language to learn.

**Can Bismuth plugins run in a decentralized manner, meaning can different Bismuth nodes run the same plugin?**

Egg: Let me rephrase. A bismuth plugin is an extra code run by a node, that is triggered by node events, like a new block, a new network difficulty, some time elapsed... The plugin code can do almost anything - and I add more hooks when there is a need. Some plugins can be autonomous (like, say, play a sound when you get an incoming transaction) Others can rely on external API (send a notification or email, add to an online doc) or run anything else (trigger a transaction on another blockchain...) plugins could also use tokens and abstract protocols on top of bismuth to work in cooperation, but this would lead us too far for today :)

**Let's say I am familiar with creating plugins for Wordpress or phpBB. How big is the learning curve to code an easy plugin for Bismuth? I assume I would need to code the plugin in Python?**

Egg: Yup, the node code is in python. So are the plugins. Python is a clear and concise language, with a great community. It's a very good choice to learn programming and do high level programs (almost all machine learning and deep learning algorithms are implemented in python) Then you need to have the hooks reference, https://github.com/bismuthfoundation/BismuthPlugins/tree/master/doc and check the demo plugins. They are very simple, only a few lines long, and commented.

**How does the design concept of a Bismuth plugin work?**

Egg: The node code does define some places where it triggers events, with optional parameters. say for instance "new block, with that block data: [....]" It sends this info to the plugin subsystem. This one then pass the info to all plugins that have registered this hook, and all related code is run.

**How is the file architecture structured and how is a plugin activated?**

Egg: All plugins reside in a "plugins" directory of the node. a plugin is a directory, with at least a __init__.py file containing the plugin code. the directory name is formated as such: 000_plugin-name with 000 begin the priority (order of registration) of the plugin. There is a convention being used, with some number being reserved: Priorities of 000-099 are reserved for low level plugins (like IFTTT api) 100-199 for demo and example plugins. 900-999 for test plugins. In between you use what you want for your own plugins.

In order to deactivate a plugin, move its directory to plugins_available So you have 2 directories:

plugins with the activated plugins

plugins.available with the unneeded plugins

plugins are loaded at node start, so for now you have to stop and restart the node if you want to (de)activate some plugin.

**What action hooks does the Bismuth plugin system support?**

Egg: See the doc https://github.com/bismuthfoundation/BismuthPlugins/tree/master/doc It's not fully up to date, but the main hooks, anyway, are "block" (new block coming, with global block info) and "fullblock" (new block, with all block transaction). This last one maybe is the most useful for any app. It allows to react the way we want to any incoming transaction.

**What could be use cases for a Bismuth plugin?**

Egg: Let's take the example of the "fullblock" hook only: It notifies you of all the transactions of every new mined block. Since you can call a plugin from a plugin (!) and you have core plugins that provide webhook and IFTTT calls, all it needs is a few lines to trigger an IFTTT or any webhook in answer to a specific incoming transaction.

- mobile notification of a pool payout
- keep track of all your transactions in a google spreadsheet
- send a digital good to a user that paid you (see dragginator use case)
- decode any extra content and act upon (Functions As a service on the blockchain... more on that later :D)

**Would Bismuth plugin applications be able to scale?**

Egg: Sure. It can be seen as a layer above bismuth, so you can imagine any architecture. master/slave apps; sharding; round robin; load balancing; different level of services...

**Is the Bismuth plugin system comparable to Ethereum contracts?**

Egg: Nope. Ethereum contract (I'm not an expert on that subject) is more like a specific code, in a specific language, that has to run on every ethereum node. The whole eth network acts as a very slow and very redundant virtual pc.

A Bismuth plugin only runs on the node you activate it onto. It adds features YOU need, and the others don't, and do not have to know you use. Bismuth plugins are extensions to the core features of a node, not a way to run some code everywhere.

You can imagine some protocol to make that. like distribute some plugin of yours, that would have a smart features inside, and all the nodes agreeing to run this plugin would work together. But it's an entirely different design and goal.

**Does every Bismuth plugin interaction needs to be recorded on the blockchain?**

Egg: No. And by default they are not. Certified plugins may come later one. That is, some plugins whose code have been audited by the team, proven to do what they say they do, then the signature of that code, and/or the plugin itself, can be stored in the chain and is "safe" to use.

**Are there demo plugins available which developers can check out and learn more about it?**

Egg: Sure, check the repo https://github.com/bismuthfoundation/BismuthPlugins and open an issue if you have an use case and lack a hook. Ping me in #dev channel on bismuth discord for any question.

**Is there a best practices cheat sheet for Bismuth plugin developers?**

Egg: Try to stick to the examples code style, but well, those are your plugins, do as you wish :)

**What are the benefits of Bismuth having a plugin system integrated?**

Egg: Imagine I would have to react to an incoming transaction with any other crypto. I would need: - make a new app - include the relevant libs to communicate with the node ( json-rpc, several layers involved) - use credentials and store them safely - poll the node (I won't get events) - debug that part - write the custom logic code I need - write more code, for instance more lib and code to trigger an IFTTT webhook - make sure this app runs 24/7 and stays connected to the node - write some logs, manage logs rotation

With bismuth plugins, all this, except the custom code logic, is already included. You can focus on YOUR needs, not on the boilerplate.

**Iyomisc, you created the first running Bismuth plugin which is already used by many users, how did you learn how the plugin system works?**

Iyomisc: Say it's the first real world use of the bismuth plugin system. I read the doc and the examples, it was easy to adapt to my needs. Also @EggdraSyl helps when I need.

**Iyomisc, how did you come up with the idea to code the dragginator plugin application?**

Iyomisc: I wanted the draggons delivery to be fully automatic. I needed to be alerted when I got a matching transaction incoming: right amount, right recipient, not already processed. The demo code was clear, that was what I needed. I added a hash store to keep track of already processed transactions, and used "send_no_gui.py" script to send back tokens and data. So my plugin is all I need, no external app. And it can be used for other similar apps.

**Iyomisc, for how long have you been working on the dragginator project?**

Iyomisc: After the idea came up, it was a few weeks of work to have the first working things. The biggest work is the website itself, the how to, all the text to write. The code itself is the fun part and is fast to do.

**Iyomisc, do you plan in the future to release some of your Bismuth plugins as open source?**

Iyomisc: My current plugin is very basic, and very close to the demo. Maybe some other plugins will be released later on, I don't know yet.

**What is your vision how Bismuth plugins could work in the future?**

Iyomisc: By definition, they will evolve. We will have more available hooks and more ready to use plugins. And I guess we will see several applications built on top on the plugins. It's really the way to go to easily interact with the Bismuth blockchain, way easier than native or json-rpc api.

Thank you very much to the developers EggdraSyl and Iyomisc for answering these questions about the powerful Bismuth plugin system and also about the working Dragginator plugin which shows that the Bismuth plugin system is already being used under real life conditions.

## Links

- Github - Bismuth Plugin
- Dragginator - Working Bismuth Plugin
- Bismuth Blockchain Explorer
- Official Bismuth Website
- Bismuth CoinmarketCap
