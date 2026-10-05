import json as _json
from typing import Any

from rmote.protocol import Tool


class JsonTool(Tool):
    @staticmethod
    def encode(data: dict[str, Any]) -> str:
        return _json.dumps(data)

    @staticmethod
    def decode(s: str) -> dict[str, Any]:
        return _json.loads(s)  # type: ignore[no-any-return]
