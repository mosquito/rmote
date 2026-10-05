"""Compile and render portable templates with registered filter classes."""

from .engine import Template, TemplateProgram, render_template
from .filters import TEMPLATE_FILTERS, TemplateFilter

__all__ = ["Template", "TemplateProgram", "render_template", "TemplateFilter", "TEMPLATE_FILTERS"]


__tool_package__ = "rmote.templates"
__tool_dependencies__ = ("rmote.template", "rmote.filters")
