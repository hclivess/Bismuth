---
title: BisBabble Interview - Nyzblossom
author: Bismuth Foundation
source: bismuthcoin.org
date: November 4, 2020
original_url: https://bismuthcoin.org/blog/2020-11-04-interview/
---

# BisBabble Interview - Nyzblossom

Tags: interview bisbabble

Q&A session with wBismuth Developer Nyzblossom.

During the past months, most of the crypto sphere has gone Uniswap crazy. Uniswap, based upon ETH smart contracts and running on Ethereum chain only, is a DEX allowing any token to be listed and exchanged, with a fast growing ecosystem and derivatives. The usual "when moon" on many discords were then replaced by "when Uniswap"?

On September 14 EggPool asked Twitter for feedback:

The related Twitter thread does provide several meaningful and useful answers, please check them 😉

Then, a third party developer team just said "let's do it". Bismuth is permission-less, the code is open, there are various repositories with integration and app examples: anyone can build on top.

## The Interview

Hello Nyzblossom, so you're the developer behind wBIS, a wrapped BIS token on Eth blockchain and were kind enough to answer our questions, welcome!

**Q: Let begin with the basics: can you tell us what your experience in the crypo field is?**

A: I am a crypto enthusiast since the early days. I am mainly speculator investor and researcher. I hired my good friend who is a coder to write wBIS to BIS gateway

**Q: What led you to Bismuth?**

A: I discovered it few months after it's Mainnet started loved the specs and started mining it then later set masternodes.

**Q: Long time fan then, cool! Do you have previous experience at wrapping tokens, What made you want to wrap Bismuth on Eth?**

A: The developer doesn't have previous experience at wrapping tokens but is familiar with writing contracts on ether. I wanted to wrap Bismuth on Eth for several reasons :

- Taking advantage of Uniswap for Bismuth needs
- To make Bis more trad-able and liquid
- To make Bis more popular
- To remove the need for any type of registration in order to buy Bis
- To find a solution for connecting Bis to ether in a trust less way and make it in a pro active way

I might be saying something wrong but this is my spirit: I think that if bismuth team can create solution for running masternodes in a trust less way then we will be able to run our ether -> bismuth and visa versa gateway also in a trust less way by holding the underlying bis used to create wBIS in this masternodes " – does it make sense?

**Q: This raises a lot of rather technical questions that would not fit in this Q&A, but definitely worth digging into!**

Can you describe the workflow of the whole process, $BIS to $wBIS and the other way around? What will it look like, from a user point of view?

A: Any transaction received by us on one of the networks (Eth, Bis) is credited on the other network. From a Bismuth user point of view, the user needs to fill the data field of the transaction with his Ethereum address. From an Ethereum user point of view, the user need to visit our website with Metamask and fill amount of token and Bismuth receive address.

**Q: Any fee in the process?**

A: Yes, there will be a fee of 20 Bis on deposit transactions (BIS -> wBis) to cover gas cost on Ethereum. No fee on withdraw transactions (wBis -> BIS).

**Q: Was diving into the Bismuth interface part easy, any stuff we could improve?**

A: I wouldn't say we dived into the Bismuth interface. Let's say we touched the surface. It was an ok experience. Parts of the documentation are fragmented and disorganized. Unifying the Bismuth knowledge base into a single place may be beneficial for new comers. Establishing and publishing coding standards and guidelines, will probably improve the overall wholeness.

**Q: Thanks for the advice!**

The dev time itself looked quite small, can you give us some feedback on how that was shared and what needed the most time?

A: It's simple and very minimal. The Bismuth side already had everything we needed (data field in each transaction). So we only needed to watch transactions for a specific address and send specific transactions from that address.

In Ethereum we added a modified erc20 transfer function, which added a data field to the transactions.

Here is the time spent in each area of development.

- Bismuth – installation, configuration, receive tx, send tx – 20%
- Ethereum – installation, configuration, receive tx, send tx – 20%
- Solidity – erc20 modification , contract deployments – 20%
- Static HTML – Metamask web3 interface – 10%
- Control Center – database of transactions – 30%

**Q: We saw you planned some maximum amount of wrapped Bismuth and a time limit as well. Can you tell us more and why?**

A: the reason for the maximum amount of wrapped Bis (200k Bis) is because it's alpha version and there are lots of things that can go wrong and we don't feel comfortable holding big sums of Bis for our users . The time limit on our operation is also because we don't want to attract big Bismuth holders and after 3 month we will decide what to do next .

**Q: Now, let's get into the core stuff. If I got it right, the workflow is not decentralized nor trustless yet, so there's an entity acting as custodian? Who is involved?**

A: Yes, there is a custodian Bismuth node that holds the underlying Bis used to issue wBIS (erc20 tokens)

**Q: Can you tell us more about your operational security? How many different keys are involved, Who has control over them, What measure did you enforce to make sure they are safe from both loss and hackers?**

A: Two keys are involved (One Bismuth, one Ethereum). They are located at the control center box, and backed up in two separate physical locations. The box is running a bismuth node, and communicates with the Ethereum blockchain via Infura. The static HMTL is hosted on gitlab.io . We are lacking front facing web pages for users to interact with. We do not have logins nor passwords. Our only interaction with our users is via the blockchain. Having such a minimal form of interaction with our users makes our attack surface exceptionally small.

**Q: A word for our users, what can you say to help them feel safe and use the bridge?**

A: I can't say it's safe to use the bridge, because it's alpha version and there might be all kinds of bugs

**Q: Are there formal guarantees you can give with respect to the bis or wbis funds safety?**

A: No

**Q: Let say other entities or users would like to run such a bridge, will you release the needed code as open source?**

Yes, we will make it open source and others also will be able to run it if they choose. We plan to release the source code after first two weeks of operation. We need to close some potential security holes first.

**Q: Thanks a lot Nyzblossom!**

The Bismuth foundation is pretty happy with this experiment. This is a definitive proof the open and permission-less nature of Bismuth leads to third parties building on top of Bismuth and bringing tools the community values. We are very grateful for the work that was done by the wBis team and wish them a great success!

## References

- wBIS has a dedicated Discord, operated by wBIS team (not Bismuth Foundation)
- wBIS should officially launch on November 9 2020.
- Web page: https://wrap-gateway.gitlab.io/wbismuth/
- Uniswap Pair https://info.uniswap.org/pair/0x7ec5b4473e5bC8815DAb476025B3534765F691ca

## Disclaimer

As stated along the interview: the wBIS bridge is not operated by the Bismuth foundation, and the Bismuth team members have absolutely no control over the bridge, nor related BIS and ETH keys. There is nothing we could do in case of something going wrong.
