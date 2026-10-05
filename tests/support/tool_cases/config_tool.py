from rmote.protocol import Tool


class ConfigTool(Tool):
    timeout: int = 30
    max_retries: int = 5
    base_url: str = "http://example.com"

    @classmethod
    def get_timeout(cls) -> int:
        return cls.timeout

    @classmethod
    def get_url(cls) -> str:
        return cls.base_url
