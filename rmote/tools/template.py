from rmote.protocol import Template as TemplateRenderer
from rmote.protocol import Tool


class Template(Tool):
    """Built-in tool that renders Mako-like templates on the remote side.

    All three methods execute on the remote process.
    See :doc:`/templating` for a full description of the template syntax.
    """

    @staticmethod
    def render(template: str, **kwargs: object) -> str:
        """Compile *template* and render it with *kwargs* as the variable namespace.

        Args:
            template: Template source string.
            **kwargs: Variables available inside the template.

        Returns:
            The rendered string.
        """
        return TemplateRenderer(template).render(**kwargs)

    @staticmethod
    def render_file(path: str, **kwargs: object) -> str:
        """Read the template file at *path* on the remote host and render it.

        Args:
            path: Absolute path to the template file on the remote filesystem.
            **kwargs: Variables available inside the template.

        Returns:
            The rendered string.
        """
        with open(path) as f:
            template = f.read()
        return TemplateRenderer(template).render(**kwargs)

    @staticmethod
    def render_compiled(template: TemplateRenderer, **kwargs: object) -> str:
        """Render a pre-compiled :class:`~rmote.protocol.Template` instance.

        Use this method when the template was compiled locally and passed as an
        argument to avoid re-compiling it on the remote side.

        Args:
            template: A :class:`~rmote.protocol.Template` instance (picklable).
            **kwargs: Variables available inside the template.

        Returns:
            The rendered string.
        """
        return template.render(**kwargs)
