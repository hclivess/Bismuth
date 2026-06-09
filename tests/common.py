"""Shared, dependency-light helpers for the test suite (no bismuthclient)."""
# Common test helpers (dependency-light; no bismuthclient).

def normalize_key(a: str) -> str:
    """Wrap a raw base64 RSA public key into PEM form (64-char lines)."""
    chunks = [a[i:i + 64] for i in range(0, len(a), 64)]
    return "\n".join(["-----BEGIN PUBLIC KEY-----", *chunks, "-----END PUBLIC KEY-----"])


def get_client(verbose: bool = False):
    """Return a connected LiteClient (regnet, port 3030). Requires a running regnet node."""
    import os
    from _lite_client import LiteClient
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return LiteClient(os.path.join(root, "wallet.der"), port=3030)
