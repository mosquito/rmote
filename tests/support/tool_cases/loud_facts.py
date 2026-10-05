"""A collector whose source text is most of its branch."""

from dataclasses import dataclass, field
from typing import Any

from rmote.protocol import Tool

LINES = 2000


@dataclass
class LoudInfo:
    available: bool
    lines: int
    raw: dict[str, Any] = field(default_factory=dict)


class LoudFacts(Tool):
    """Owns the ``loud`` key. Its text is kept in ``raw`` unless refused."""

    key = "loud"
    version = 1
    result_type = LoudInfo

    @staticmethod
    def collect(*, raw: bool = True) -> LoudInfo:
        text = "measured line\n" * LINES
        return LoudInfo(available=True, lines=text.count("\n"), raw={"text": text} if raw else {})
