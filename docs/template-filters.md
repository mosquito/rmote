# Writing template filters

A filter is a Python class derived from `TemplateFilter`. Register the class
under a name in `Template(..., filters=...)`, then use that name after `|` in
an expression. Filters are the extension point for operations that the
restricted template language cannot perform directly.

This guide builds a filter that turns labels into identifiers, then sends the
compiled template and its filter to another Python interpreter. For the
language and built-in filters, see {doc}`templating`.

## Define a filter in a Python file

Save the following class in `template_filters.py`. The complete example module
is in `examples/tools/template_filters.py` in the repository.

```{literalinclude} ../examples/tools/template_filters.py
:language: python
:start-at: from __future__
:end-before: class ItemsFilter
```

`__call__` receives the piped value first. Any arguments written inside the
filter's parentheses follow it. Here, `separator` has a default, so callers can
omit it, pass it positionally, or pass it by name. The return value becomes the
input of the next filter in the chain; when the final expression is inserted
into text, the engine converts it to a string.

The class keeps its regular-expression pattern on the class and imports `re`
inside the method. Those choices also make it portable: its implementation
does not rely on a module global being present on the receiving interpreter.

## Register and call the filter

Save this client beside `template_filters.py`:

<!-- name: test_filter_registration; fixtures: tool_examples -->
```python
from template_filters import SlugFilter
from rmote.templates import TEMPLATE_FILTERS, Template

filters = {**TEMPLATE_FILTERS, "slug": SlugFilter}
template = Template("service={{ name | slug | upper }}", filters=filters)
assert template.render(name=" Front End 01 ") == "service=FRONT-END-01"

underscores = Template("{{ name | slug('_') }}", filters=filters)
assert underscores.render(name="Front End 01") == "front_end_01"
keyword = Template("{{ name | slug(separator=separator) }}", filters=filters)
assert keyword.render(name="Front End 01", separator="_") == "front_end_01"
```

The filter name must be a public identifier, such as `slug` or `service_name`;
names starting with `_`, Python keywords, and punctuation are rejected. The
mapping value is `SlugFilter`, **not** `SlugFilter()` or a function. The engine
constructs the instances itself.

For this class, `name | slug('_')` calls `instance(name, '_')`. Arguments are
ordinary template expressions and can refer to context variables. Use Python
defaults and keyword-only parameters in `__call__` to define your filter's API.
Filter calls are synchronous: implement a regular `def __call__`, and do any
asynchronous data collection in the tool before rendering.

| Registration | Available filters |
|---|---|
| Omit `filters` | All entries of `TEMPLATE_FILTERS` |
| `filters={"slug": SlugFilter}` | Only `slug` |
| `filters={**TEMPLATE_FILTERS, "slug": SlugFilter}` | Built-ins plus `slug` |
| `filters={**TEMPLATE_FILTERS, "lower": MyLowerFilter}` | Built-ins with `lower` replaced |
| `filters={}` | None |

An explicit mapping completely replaces the defaults. `TEMPLATE_FILTERS` is
read-only; create a new dictionary when extending it. A template snapshots the
mapping at construction, so changing your dictionary later does not change an
already compiled template. Create another `Template` to use a different mapping.
Unknown filter names fail at construction; missing or unexpected call arguments
fail during rendering when Python invokes `__call__`.

The `render_template` helper and `RenderTemplate.render` with a source string
use the built-ins. Register custom filters by constructing a `Template` and
passing that object to the remote tool.

## Instance state and input data

The engine calls each registered class with no constructor arguments. It creates
one instance **per registered name per render**, including names that are not
used by that template. Keep constructors cheap. All calls to a filter under the
same name share that instance during one render; the next render starts over.

State can be useful for numbering output. This local example makes that lifetime
visible:

<!-- name: test_filter_instance_state -->
```python
from rmote.templates import Template, TemplateFilter

class NumberedFilter(TemplateFilter):
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, value: str) -> str:
        self.count += 1
        return f"{self.count}:{value}"

template = Template(
    "{% for name in names %}{{ name | numbered }};{% endfor %}",
    filters={"numbered": NumberedFilter},
)
assert template.render(names=["web", "db"]) == "1:web;2:db;"
assert template.render(names=["cache"]) == "1:cache;"
```

Use call arguments or context variables for configuration that changes between
hosts. Mutating a class attribute on the controller is not a portable way to
configure a remote filter: transfer carries the class's source definition.

Filters receive the original context values. Input dictionaries and lists are
validated without copying, so changing them inside a filter changes the caller's
data too. Prefer returning a new value. Plain input data is intentionally limited;
convert dataclasses with `asdict` and generators with `list` before rendering.

## Return collections and objects

A filter may return a string, number, list, dictionary, or a trusted Python
object. Return the value the next operation needs; there is no need to stringify
a list that the template will loop over. Public attributes of a returned object
are available through dot lookup, as with the built-in `ipaddress` filter.
Private attributes and method calls remain forbidden in template expressions.

Iterator results are consumed and materialized, including iterators nested in
returned containers. This lets the template reuse the result or ask for its
length. Return finite iterators; rendering consumes them before proceeding.
For example, add this class to `template_filters.py` to expose dictionary items:

```{literalinclude} ../examples/tools/template_filters.py
:language: python
:pyobject: ItemsFilter
```

The complete file imports `Iterator` from `collections.abc` for its return
annotation. The template below reuses the materialized pairs for a length and a
loop, without calling `settings.items()` inside the template:

<!-- name: test_filter_collection_result; fixtures: tool_examples -->
```python
from template_filters import ItemsFilter
from rmote.templates import TEMPLATE_FILTERS, Template

template = Template(
    "{% set pairs = settings | items %}"
    "count={{ pairs | length }};"
    "{% for name, value in pairs %}{{ name }}={{ value }};{% endfor %}",
    filters={**TEMPLATE_FILTERS, "items": ItemsFilter},
)
assert template.render(settings={"port": 8080, "workers": 4}) == "count=2;port=8080;workers=4;"
```

Registered filters and their results are trusted. Accessing a result's property,
consuming its iterator, or converting it to a string can execute its Python code.
The restricted expression language is not a sandbox for an untrusted filter.

## Validate values and handle missing input

Raise an ordinary exception when input is invalid. For example, `SlugFilter`
raises `ValueError` for an unsupported separator. The exception propagates to
the caller; `default` does not catch errors thrown by a preceding filter.

<!-- name: test_filter_validation; fixtures: tool_examples -->
```python
from template_filters import SlugFilter
from rmote.templates import TEMPLATE_FILTERS, Template

template = Template(
    "{{ name | slug(separator='.') | default('fallback') }}",
    filters={**TEMPLATE_FILTERS, "slug": SlugFilter},
)
try:
    template.render(name="Front End")
except ValueError as error:
    assert "separator" in str(error)
else:
    raise AssertionError("default must not hide an invalid filter argument")
```

Normally, a missing input raises an error before your filter is called. Put
`default` first when you want to supply a fallback:
`{{ name | default('Unnamed Service') | slug }}`.

A filter that deliberately handles absent values can set
`handles_undefined = True`. It then receives a `TemplateUndefined` sentinel for
missing simple lookups. Do not test that sentinel with `if value` or convert it
to a string: both operations re-raise the original lookup error. Check its type.
Add this class to `template_filters.py` to give required settings a named error:

```{literalinclude} ../examples/tools/template_filters.py
:language: python
:pyobject: RequiredFilter
```

<!-- name: test_filter_required_value; fixtures: tool_examples -->
```python
from template_filters import RequiredFilter
from rmote.templates import Template

template = Template(
    "{{ settings.port | required('service port') }}",
    filters={"required": RequiredFilter},
)
assert template.render(settings={"port": 8080}) == "8080"
assert template.render(settings={"port": 0}) == "0"
for settings in ({}, {"port": None}):
    try:
        template.render(settings=settings)
    except ValueError as error:
        assert str(error) == "service port is required"
    else:
        raise AssertionError("A missing or None port must fail validation")
```

This opt-in concerns lookup failures in the piped input. It does not catch
arithmetic errors, failures in filter arguments, or arbitrary exceptions from
other filters. `TemplateUndefined.error` holds the original lookup exception if
you want to re-raise it.

## Make the class portable

Custom filter serialization carries the class source and its filter base
classes. The remote interpreter reconstructs the class without importing the
user module where it was originally defined. This differs from transferring a
whole module as a Tool.

Keep these requirements in mind when writing that source:

- Define the class in a readable `.py` file. REPL, notebook, or dynamically
  executed definitions may work locally but have no source available for transfer.
- Put runtime imports inside the class or its methods, and constants on the
  class. Closure variables, module globals, and external decorators are not
  captured. Reference class attributes through `self` or `type(self)`.
- Filter inheritance is supported; all direct bases must also be
  `TemplateFilter` subclasses. Arbitrary mixin classes are not transferred by
  this serializer.
- External dependencies used by the filter must be available on the target.
  The engine and the examples here require only the standard library.
- Keep the code compatible with the target's Python version. Source transfer
  does not convert Python syntax between versions.

Classes defined inside functions can be transferred when their source is
available, but variables captured from the enclosing function are still not
included. A top-level class in a small module is usually easier to maintain.

Pickle round-tripping is a useful first check. It exercises the class source
and the stored template program without reparsing the template:

<!-- name: test_filter_pickle; fixtures: tool_examples -->
```python
import pickle
from template_filters import SlugFilter
from rmote.templates import Template

compiled = Template("{{ name | slug }}", filters={"slug": SlugFilter})
restored = pickle.loads(pickle.dumps(compiled))
assert restored.render(name="Front End") == "front-end"
```

Also test a real remote call: an in-process pickle check alone does not prove
that external dependencies are present on the target. Only unpickle trusted
payloads; restoring a custom filter executes its class source.

## Render remotely with your filters

The following example renders in a separate local Python process. Use
`Connection.from_ssh("user@server")` in the same place to render on an SSH host,
or select another of rmote's {doc}`transports`:

<!-- name: test_custom_filter_remote; fixtures: tool_examples, client_resources; mark: timeout(20) -->
```python
from template_filters import ItemsFilter, RequiredFilter, SlugFilter
from rmote.templates import TEMPLATE_FILTERS, Template
from rmote.sync import Connection
from rmote.tools.template import RenderTemplate

compiled = Template(
    "service={{ name | required('service name') | slug | upper }};"
    "{% for key, value in settings | items %}{{ key }}={{ value }};{% endfor %}",
    filters={
        **TEMPLATE_FILTERS,
        "slug": SlugFilter,
        "items": ItemsFilter,
        "required": RequiredFilter,
    },
)
with Connection.from_local() as remote:
    result = remote(
        RenderTemplate.render, compiled,
        name="Front End", settings={"port": 8080},
    )
    assert result == "service=FRONT-END;port=8080;"
```

Pass the `Template` object itself. Its compiled program, registration mapping,
and custom filter definitions travel together. rmote transfers the engine
package lazily; the target does not need `template_filters.py` installed.
The string result comes back through the ordinary RPC response.

With the asynchronous client, await the render call:

<!-- name: test_custom_filter_remote_async; fixtures: tool_examples, docs_ssh; mark: timeout(20) -->
```python
import asyncio
from template_filters import SlugFilter
from rmote.protocol import Protocol
from rmote.templates import Template
from rmote.tools.template import RenderTemplate

async def main() -> str:
    compiled = Template("{{ name | slug }}", filters={"slug": SlugFilter})
    async with await Protocol.from_ssh("user@server") as remote:
        return await remote(RenderTemplate.render, compiled, name="Front End")

assert asyncio.run(main()) == "front-end"
```

These calls receive context values from the controller. To use data that only
the target can read, pass the compiled `Template` to your own Tool method,
collect the values there, and call `template.render(**values)` inside that
method. The same filter and program transfer rules apply. See
{doc}`writing-tools` for defining the method and {doc}`api/filters` for the
filter class reference.
