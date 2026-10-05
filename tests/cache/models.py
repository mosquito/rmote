from dataclasses import dataclass
from typing import Any


@dataclass
class Payload:
    values: dict[str, Any]
