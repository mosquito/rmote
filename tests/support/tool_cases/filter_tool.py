"""Tools and filter classes used to exercise source transfer."""

from rmote.protocol import Tool
from rmote.templates import Template
from rmote.templates.filters import TEMPLATE_FILTERS, TemplateFilter


class PrefixFilter(TemplateFilter):
    prefix = "host:"

    def __call__(self, value: object, suffix: str = "") -> str:
        return self.prefix + str(value) + suffix


class HostFilter(PrefixFilter):
    def __call__(self, value: object, suffix: str = "") -> str:
        import re

        return super().__call__(re.sub(r"\s+", "-", str(value).lower()), suffix)


class FilterTemplateTool(Tool):
    @staticmethod
    def package_info():
        import rmote.templates as package
        from rmote.filters import TemplateFilter as LegacyFilter
        from rmote.template import Template as LegacyTemplate
        from rmote.templates import engine, filters

        assert Template is LegacyTemplate is package.Template
        assert TemplateFilter is LegacyFilter is package.TemplateFilter
        assert package.__spec__.submodule_search_locations is not None
        assert package.__path__ == []
        return package.__package__, engine.__package__, filters.__package__, Template.__module__

    @staticmethod
    def restore(payload):
        import pickle

        return pickle.loads(payload)

    @staticmethod
    def render(name):
        return Template(
            "{{ name|custom|upper }}",
            filters={"custom": HostFilter, "upper": TEMPLATE_FILTERS["upper"]},
        ).render(name=name)
