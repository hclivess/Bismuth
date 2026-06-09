"""Per-connection client state for the Bismuth node.

A :class:`Client` instance is created for each outbound/inbound peer connection
and threaded through the connection handlers as a tiny mutable flag holder. Its
single ``connected`` boolean is flipped to ``True`` once the handshake / sync
loop for that peer is running and back to ``False`` (or simply left to be
garbage-collected) when the loop exits, so the surrounding code can tell whether
a given worker connection is still live.

Instances are built at ``node.py`` (``client.Client()``) and
``worker.py`` (``client.Client()``). Behaviour here is intentionally minimal;
other modules may set further attributes on the instance dynamically.
"""


class Client:
    """Holds the liveness flag for a single peer connection.

    Attributes:
        connected (bool): ``True`` while the connection's handshake/sync loop is
            active, ``False`` otherwise. Starts ``False``.
    """

    def __init__(self) -> None:
        self.connected = False
