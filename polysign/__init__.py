"""polysign — Bismuth's pluggable signature / address scheme package.

Provides a common :class:`~polysign.signer.Signer` abstraction with concrete
implementations (RSA, ECDSA/secp256k1, Ed25519, ML-DSA post-quantum, secp256r1,
plus BTC/CRW test helpers) and a :class:`~polysign.signerfactory.SignerFactory`
that dispatches by signer type or address. Vendored in-tree as part of the core
node (see doc/14).
"""

__version__ = '0.1.4'
