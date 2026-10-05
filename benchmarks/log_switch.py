"""A tool that chooses how the records of the remote side travel.

A benchmark needs both ways in one process: the load of the machine changes
between two runs, so only rounds that alternate can be compared.
"""

import logging
from typing import Any

from rmote.protocol import Tool


class LogSwitch(Tool):
    @staticmethod
    def attach(enabled: bool) -> bool:
        """Let the responses carry the records, or give every batch a packet.

        Raises:
            RuntimeError: This process has no remote log handler, so it is not
                the remote side of a connection.
        """
        for item in logging.getLogger().handlers:
            handler: Any = item
            protocol = getattr(handler, "protocol", None)
            if protocol is None:
                continue
            if enabled:
                protocol.log_handler = handler
                # Restore the choice between a response and the timer.
                handler.__dict__.pop("start", None)
            else:
                protocol.log_handler = None
                # Send at once, as the handler did before the records could
                # travel with a response.
                handler.start = handler.send_alone
            return enabled
        raise RuntimeError("This process has no remote log handler")
