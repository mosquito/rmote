"""Portable filter classes used in the template-filter guide."""

from __future__ import annotations

from collections.abc import Iterator

from rmote.templates import TemplateFilter


class SlugFilter(TemplateFilter):
    """Turn a label into an ASCII identifier separated by dashes or underscores."""

    pattern = r"[^a-z0-9]+"

    def __call__(self, value: str, separator: str = "-") -> str:
        import re

        if separator not in ("-", "_"):
            raise ValueError("separator must be '-' or '_'")
        return re.sub(self.pattern, separator, value.casefold()).strip(separator)


class ItemsFilter(TemplateFilter):
    """Expose dictionary items for unpacking in template loops."""

    def __call__(self, value: dict[str, object]) -> Iterator[tuple[str, object]]:
        return iter(value.items())


class RequiredFilter(TemplateFilter):
    """Raise a named validation error for missing values or None."""

    handles_undefined = True

    def __call__(self, value: object, label: str) -> object:
        from rmote.templates.filters import TemplateUndefined

        if isinstance(value, TemplateUndefined) or value is None:
            raise ValueError(f"{label} is required")
        return value
