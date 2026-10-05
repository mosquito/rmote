# Template compiler and interpreter

Import `Template`, `TemplateProgram`, `render_template`, `TemplateFilter`, and
`TEMPLATE_FILTERS` from `rmote.templates`. The classes below implement the
compiled representation; the package exports the same class objects.

See {doc}`../templating` for the restricted language, trust boundaries, and
examples. Filters are documented in {doc}`filters`.

```{eval-rst}
.. automodule:: rmote.templates.engine
   :members:
   :special-members: __call__, __reduce__
```
