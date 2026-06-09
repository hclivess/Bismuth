"""Logger holder object for the Bismuth node.

A single :class:`Logger` instance lives on the node as ``node.logger``. It is a
thin container whose ``app_log`` attribute is filled in at startup with the
configured :class:`logging.Logger` returned by ``log.log(...)`` (see ``log.py``).
Code throughout the node then logs via ``node.logger.app_log.info(...)`` /
``.warning(...)`` / ``.error(...)``.

This indirection (a holder object rather than a bare logger) lets the logger be
attached to the node state object and passed around with it, and lets the
underlying ``app_log`` be (re)assigned after the :class:`Logger` is constructed.

Instantiated in ``node.py`` (``node.logger = logger.Logger()``) and in
``tokensv2.py`` when run standalone. NOTE: importers use the package form
``from libs import logger``; a search for ``import libs.logger`` will miss them,
so this module is **not** dead despite appearing unused at first glance.
"""


class Logger:
    """Container for the node's application logger.

    Attributes:
        app_log (logging.Logger | None): The configured logger, set at startup
            via ``log.log(...)``. ``None`` until then.
    """

    def __init__(self) -> None:
        self.app_log = None
