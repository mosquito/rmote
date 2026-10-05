from __future__ import annotations

from pathlib import Path

from rmote.protocol import Tool
from rmote.templates.engine import Template


class RenderTemplate(Tool):
    """Built-in tool that renders Jinja-like templates with restricted expressions.

    Both methods execute on the remote process.
    See :doc:`/templating` for a full description of the template syntax.


    Render locally, then send the compiled template to another Python process::

        >>> from rmote.templates import Template
        >>> from rmote.sync import Connection
        >>> from rmote.tools import RenderTemplate
        >>> Template("Hello, {{ name }}!").render(name="Ada")
        'Hello, Ada!'
        >>> compiled = Template("port={{ port }}")
        >>> with Connection.from_local() as remote:
        ...     rendered = remote(RenderTemplate.render, compiled, port=8080)
        >>> rendered
        'port=8080'
        >>> RenderTemplate.render("{% for name in names %}[{{ name }}]{% endfor %}", names=["web", "db"])
        '[web][db]'
    """

    @staticmethod
    def render(template: str | Template, **kwargs: object) -> str:
        """Render source text or a compiled template with the supplied values.

        Source strings are compiled on the receiving host. A supplied
        :class:`~rmote.templates.engine.Template` transfers its intermediate
        representation and filter classes, and renders without recompilation.

        Args:
            template: Source string or a compiled template instance.
            **kwargs: Variables available inside the template.

        Returns:
            The rendered string.
        """
        if isinstance(template, str):
            template = Template(template)
        return template.render(**kwargs)

    @staticmethod
    def render_file(path: str | Path, **kwargs: object) -> str:
        """Read the template file at *path* on the remote host and render it.

        Args:
            path: Absolute path to the remote template file, as a string or Path.
            **kwargs: Variables available inside the template.

        Returns:
            The rendered string.
        """
        path = Path(path)
        return Template(path.read_text()).render(**kwargs)
