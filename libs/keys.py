"""Wallet key material holder for the Bismuth node.

A single :class:`Keys` instance lives on the node as ``node.keys``. It is a
plain container that ``node_init.load_keys`` fills in from the wallet file via
``essentials.keys_load(...)``. The node then signs blocks/transactions with
``node.keys.key`` and advertises ``node.keys.address``.

``__init__`` declares the four attributes that exist before the wallet is
loaded. Two further attributes are attached dynamically by ``load_keys`` and are
documented below for completeness:

    * ``key``                  -- the private-key object used for signing
    * ``private_key_readable`` -- PEM/text form of the private key

(See ``node_init.py`` ``load_keys`` for the exact unpacking.)
"""


class Keys:
    """Holds the node's wallet key material.

    Attributes set here (pre-load placeholders):
        public_key_readable (str | None): PEM/text form of the public key.
        public_key_b64encoded (bytes | str | None): Base64-encoded public key,
            as embedded in signed transactions.
        address (str | None): The node's wallet address derived from the key.
        keyfile (str | None): Path to the wallet file the keys were loaded from.

    Attributes attached later by ``node_init.load_keys`` (not declared here):
        key: The private-key object used to sign blocks/transactions.
        private_key_readable (str): PEM/text form of the private key.
    """

    def __init__(self) -> None:
        self.public_key_readable = None
        self.public_key_b64encoded = None
        self.address = None
        self.keyfile = None
