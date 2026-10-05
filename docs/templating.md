# Templating

Use `Template` to render configuration text with **Jinja-like delimiters and
restricted expressions**. The engine runs locally and in remote tools without an
extra package. The {doc}`quickstart` uses it to create a systemd service unit.

## Why rmote has its own template engine

Templates need to be **portable over the wire**. A controller can pass a compiled
`Template` as an RPC argument, and a remote tool can render it using values
collected on that host. This must work with only Python and its standard library
on the target, without a separate package installation step.

rmote's engine is designed for this transfer: its code is sent lazily when
needed, and a template's compiled intermediate representation and registered
filter classes can travel with the call. The remote side renders the prepared
template without parsing it again.

Rendering with Jinja2 on the target would also require making its runtime and
dependencies available there. Keeping the built-in engine self-contained lets
rmote provide remote rendering through its existing code-transfer mechanism.
The Jinja-like syntax offers familiar expressions, conditions, loops, and
filters; it does not imply full Jinja2 compatibility. Applications can still
render with Jinja2 locally and send the resulting text to the remote tool.

## Using the engine

`rmote.templates` exports `Template`, `TemplateProgram`, `TemplateFilter`,
`TEMPLATE_FILTERS`, and `render_template`. The engine and built-in filters live
in `rmote.templates.engine` and `rmote.templates.filters`. The connection lazily
transfers this package to the target; no remote installation is needed.

Construct a template once and pass named values to `render()`. The result is a
string. Reuse the same object for multiple sets of values; each render gets fresh
variable bindings:

<!-- name: test_template_reuse -->
```python
from rmote.templates import Template

service = Template("listen {{ host }}:{{ port }}{% if tls %} ssl{% endif %};")
assert service.render(host="127.0.0.1", port=8080, tls=False) == "listen 127.0.0.1:8080;"
values = {"host": "0.0.0.0", "port": 443, "tls": True}
assert service.render(**values) == "listen 0.0.0.0:443 ssl;"
```

Render locally when all values are known to the client. Render in a remote
tool when values come from that host. You can also use another template engine
locally and send the resulting string to the target.

| Entry point | Compilation and execution |
|---|---|
| `Template(source).render(**values)` | Compile locally, then reuse the object locally or send it to a tool |
| `render_template(source, **values)` | Compile and render locally with the built-in filters |
| `remote(RenderTemplate.render, compiled, **values)` | Transfer a prepared `Template` and render on the target |
| `remote(RenderTemplate.render, source, **values)` | Compile and render the source on the target with built-in filters |
| `remote(RenderTemplate.render_file, path, **values)` | Read and render a template file on the target |

The `remote(...)` rows use the synchronous client. With `Protocol`, await the
same calls. See the remote examples below for connection setup.

## Context and missing values

Template source is a restricted data language. It cannot import modules, call
Python functions or methods, define classes, or access private attributes.
Context values must be plain built-in data: strings, bytes, numbers, booleans,
`None`, lists, tuples, dictionaries, sets, frozensets, and ranges. Application objects,
callables, and subclasses with custom behavior are rejected without invoking
their properties or conversion methods. Pass their data explicitly instead.

A value is checked when the template first reads its name. A value the template
never reads is never checked, so an unused context entry neither costs time nor
raises. The check walks containers, so its cost grows with the size of the data
that the template reads. To render the same large data many times, freeze it
once with {func}`rmote.immutable.freeze` and pass the snapshot as the only
positional argument: a frozen snapshot holds plain data by construction and is
accepted without a walk. A frozen mapping is a dict subclass, so reading a
field of it costs what reading a dictionary costs: a loop over many small
records is faster on a snapshot, not slower.

<!-- name: test_frozen_context -->
```python
from rmote.immutable import freeze
from rmote.templates import Template

template = Template("{{ hosts|length }} hosts on {{ hostname }}")
snapshot = freeze({"hosts": [{"name": "one"}, {"name": "two"}], "hostname": "controller"})
assert template.render(snapshot) == "2 hosts on controller"
```

Registered filter classes and pickle payloads are **trusted code**. A filter may
execute Python, and unpickling transferred filter definitions executes their
source. Do not accept filter classes or pickle bytes from untrusted users.
The restricted language does not provide CPU or memory isolation.

Values are not escaped for shells, HTML, or configuration formats. Escape them
for the output format you generate. Missing values raise errors unless handled
by `default`.

Pass dataclasses, including fact models, as dictionaries using `asdict`. Dot
lookup on a dictionary reads a key; it does not call a dictionary method. Use
bracket lookup for computed keys or keys containing punctuation:

<!-- name: test_template_context -->
```python
from dataclasses import asdict, dataclass
from rmote.templates import Template

@dataclass
class Service:
    name: str
    port: int

template = Template("{{ service.name }}:{{ service.port }} {{ labels['app-name'] }}")
assert template.render(
    service=asdict(Service("web", 8080)), labels={"app-name": "frontend"},
) == "web:8080 frontend"
```

For a facts snapshot, prepare `{key: asdict(value) for key, value in snapshot.items()}`
and pass the resulting dictionary. See {doc}`api/tools/facts` for collecting that snapshot.
Convert a generator to a list before using it as an input value. Cyclic containers
are rejected; the context must be finite plain data.

A missing variable is different from `None` or an empty value. A direct lookup
raises an error. Apply `default` before further processing when a value is
optional, including a missing key in a nested dictionary:

<!-- name: test_template_optional_context -->
```python
from rmote.templates import Template

label = Template("{{ settings.host | default('localhost') | upper }}")
assert label.render(settings={}) == "LOCALHOST"
assert label.render(settings={"host": "web"}) == "WEB"
assert Template("{{ value | default('fallback') }}").render(value=None) == "None"
assert Template("{{ value | default('fallback', boolean=True) }}").render(value="") == "fallback"
```

`value or 'fallback'` still requires `value` to exist. `default` handles missing
lookups, but does not hide division errors, invalid filter arguments, or exceptions
raised by a filter. Use `default(..., boolean=True)` only when false values such
as `0`, `False`, and an empty collection should also use the fallback.

## Expressions

Wrap an expression in `{{ … }}` to insert its string representation. The
language supports literals, lists/tuples/dictionaries/sets, indexing and slices,
public attribute lookup, arithmetic, comparisons, `and`/`or`/`not`, conditional
expressions, and registered filters. Both `True`/`False`/`None` and
`true`/`false`/`none` are accepted. Dictionary keys can be read as `data['key']`
or `data.key`. Private object attributes, calls, comprehensions, and f-strings
are rejected during construction:

<!-- name: test_interpolation -->
```python
from rmote.templates import Template

assert Template("Hello, {{ name }}!").render(name="Alice") == "Hello, Alice!"
keys = Template("Keys: {{ d | sort | join(', ') }}")
assert keys.render(d={"b": 2, "a": 1}) == "Keys: a, b"
assert Template("{{ {'key': 42}['key'] }}").render() == "42"
```

A comma creates a tuple with or without parentheses: `{{ a, b }}` renders
`(1, 2)` when `a=1` and `b=2`. The same syntax works in assignments:

<!-- name: test_tuple_expressions -->
```python
from rmote.templates import Template

assert Template("{{ a, b }}").render(a=1, b=2) == "(1, 2)"
assert Template("{% set pair = 1, 2 %}{{ pair | join(':') }}").render() == "1:2"
```

Both expressions and commands may span multiple source lines. Delimiters
inside quoted strings do not terminate a tag:

<!-- name: test_multiline_expression -->
```python
from rmote.templates import Template

template = Template("""{{
    numbers | join(', ')
}}""")
assert template.render(numbers=[1, 2, 3]) == "1, 2, 3"
assert Template("{{ '}}' }}").render() == "}}"
```

## Filters

Use `|` to apply a filter to a value. Filters accept positional and keyword
arguments, and chains run from left to right. They work in expressions,
conditions, loops, and assignments:

<!-- name: test_filters -->
```python
from rmote.templates import Template

hosts = Template("{{ hosts | unique | sort | join(', ') }}")
assert hosts.render(hosts=["web", "db", "web"]) == "db, web"
assert Template("{{ ' ff ' | trim | int(base=16) }}").render() == "255"
template = Template(
    "{% set names = hosts | unique | sort %}"
    "{% if names | length %}"
    "{% for name in names | reverse %}{{ name | upper }};{% endfor %}"
    "{% endif %}"
)
assert template.render(hosts=["web", "db", "web"]) == "WEB;DB;"
```

Filters bind before arithmetic and comparisons: `1 + '2' | int` is `3`.
Use parentheses to filter a whole expression, such as `(a + b) | string`.
`|` is reserved for filters; compute bitwise operations before rendering or
implement them in a registered filter. Pipes inside quoted strings remain
literal text. Unknown filters raise `SyntaxError` at compilation.

The default mapping, `TEMPLATE_FILTERS`, contains these filters. The piped
value is the first argument and is omitted from the signatures below:

| Filters | Behavior |
|---|---|
| `lower`, `upper`, `capitalize` | Convert to a string and change its case |
| `trim(chars=None)` | Strip whitespace or a specified set of characters |
| `replace(old, new, count=None)` | Replace all occurrences, or at most `count` |
| `join(d='', attribute=None)` | Join stringified items with a separator |
| `length`, `count` | Length of a sized value |
| `default(default_value='', boolean=False)`, `d` | Substitute for a missing value; also for false values when `boolean=True` |
| `sort(reverse=False, case_sensitive=False, attribute=None)` | Return a sorted list |
| `unique(case_sensitive=False, attribute=None)` | Yield the first item for each distinct, hashable key |
| `first`, `last` | Select an item; empty sequences can be handled by `default` |
| `reverse`, `list` | Reverse an iterable or materialize it as a list |
| `int(default=0, base=10)`, `float(default=0.0)`, `string` | Convert a value |
| `tojson(indent=None)` | JSON with sorted keys and escaped `<`, `>`, `&`, and apostrophes |
| `ipaddress(action=None)` | Parse with `ipaddress.ip_interface` and optionally select an operation |

`join`, `sort`, and `unique` accept dotted `attribute` paths through dictionary
keys, public attributes of trusted results, and numeric list indices, such as `'addresses.0'`.
`sort` also accepts comma-separated paths for multiple sort keys. String sort
and uniqueness are case-insensitive by default. The interpreter materializes
iterator results from trusted filters, so they can be reused and passed to
`length` or `last`. `last` requires a reversible sequence.

`default` handles a missing variable, attribute, dictionary key, or index in
a simple lookup. It does not suppress errors from filter code or arithmetic.
`None`, `False`, zero, and empty collections are retained unless `boolean=True`:

<!-- name: test_filter_default -->
```python
from rmote.templates import Template

assert Template("{{ missing | default('localhost') }}").render() == "localhost"
assert Template("{{ None | default('localhost') }}").render() == "None"
assert Template("{{ '' | default('localhost', True) }}").render() == "localhost"
assert Template("{{ [] | first | default('empty') }}").render() == "empty"
```

`ipaddress` returns an `ipaddress.ip_interface` object by default, retaining
the prefix. Pass `action` to select ordinary data in the filter: `interface`,
`address`, `network`, `network_address`, `netmask`, `hostmask`, and `broadcast`
return strings; `prefix` and `version` return integers. Invalid addresses or
unknown actions raise `ValueError`. Like other trusted filter results, the
default object also exposes its public attributes.

<!-- name: test_filter_ipaddress -->
```python
from rmote.templates import Template

template = Template(
    "address={{ address | ipaddress('address') }} "
    "network={{ address | ipaddress('network') }}"
)
assert template.render(address="192.0.2.7/24") == "address=192.0.2.7 network=192.0.2.0/24"
assert Template("{{ address | ipaddress('network') }}").render(address="2001:db8::123/64") == "2001:db8::/64"
assert Template("{{ address | ipaddress(action='prefix') + 1 }}").render(address="192.0.2.7/24") == "25"
```

### Custom filter classes

Pass a dictionary of names to `TemplateFilter` subclasses. Implement
`__call__(self, value, ...)` to transform the piped value. The read-only global mapping
`TEMPLATE_FILTERS` is used only when `filters` is omitted. An explicit
dictionary supplies the entire set of filters; `{}` disables all filters.
To extend the defaults, merge them explicitly:

<!-- name: test_custom_filters -->
```python
from rmote.templates import TEMPLATE_FILTERS, Template, TemplateFilter

class PrefixFilter(TemplateFilter):
    def __call__(self, value, prefix="host:"):
        return prefix + str(value)

template = Template("{{ name | prefix }}", filters={"prefix": PrefixFilter})
assert template.render(name="web") == "host:web"
extended = Template(
    "{{ name | prefix('node:') | upper }}",
    filters={**TEMPLATE_FILTERS, "prefix": PrefixFilter},
)
assert extended.render(name="web") == "NODE:WEB"
```

The mapping contains **classes**, not instances or plain functions. Constructors
accept no arguments; the template creates fresh instances for each render.
Registration happens when constructing `Template`, and unknown filter names are
compilation errors.

See {doc}`template-filters` for a complete filter module, positional and keyword
arguments, registration choices, instance state, iterator results, missing values,
and tested examples of sending custom filters to a remote interpreter.

## Conditions and Loops

Use `{% … %}` for control flow, whether inside a text line or on its own line.
Blocks can nest, and a trailing colon is optional:

<!-- name: test_inline_control_flow -->
```python
from rmote.templates import Template

listen = Template("listen {{ port }}{% if ssl %} ssl{% endif %};")
assert listen.render(port=443, ssl=True) == "listen 443 ssl;"
assert listen.render(port=80, ssl=False) == "listen 80;"

hosts = Template("hosts={% for host in hosts %}[{{ host }}]{% endfor %};")
assert hosts.render(hosts=["web", "db"]) == "hosts=[web][db];"
assert hosts.render(hosts=[]) == "hosts=;"
```

Branches use `elif` and `else`. Loops support `break`, `continue`, and an
`else` branch, which runs after normal completion (including an empty loop)
but not after `break`. Close blocks with `endif`, `endfor`, or the generic `end`.
Only `if`, `for`, `set`, `break`, and `continue` commands are supported.

Loop targets can unpack pairs. A dictionary yields its keys, so use an explicit
lookup for values, or prepare a list of pairs in Python. There is no implicit
`loop` object; pass indices from Python or maintain a counter with `set`:

<!-- name: test_template_loop_pairs -->
```python
from rmote.templates import Template

ports = Template("{% for name, port in ports %}{{ name }}={{ port }};{% endfor %}")
assert ports.render(ports=[("http", 80), ("https", 443)]) == "http=80;https=443;"
services = Template(
    "{% for name in services | sort %}{{ name }}={{ services[name] }};{% endfor %}"
)
assert services.render(services={"web": 8080, "db": 5432}) == "db=5432;web=8080;"
```

The `for`/`else` behavior follows normal loop completion: it also runs after a
nonempty loop when no `break` occurs. `continue` skips the current iteration:

<!-- name: test_template_loop_completion -->
```python
from rmote.templates import Template

numbers = Template(
    "{% for n in numbers %}"
    "{% if n < 0 %}{% continue %}{% endif %}"
    "{% if n == 0 %}{% break %}{% endif %}"
    "{{ n }};"
    "{% else %}done{% endfor %}"
)
assert numbers.render(numbers=[2, -1, 3]) == "2;3;done"
assert numbers.render(numbers=[2, 0, 3]) == "2;"
assert numbers.render(numbers=[]) == "done"
```

<!-- name: test_if_elif_else -->
```python
from rmote.templates import Template

template = Template(
    "{% if n > 0 %}positive{% elif n == 0 %}zero{% else %}negative{% endif %}"
)
assert template.render(n=1) == "positive"
assert template.render(n=0) == "zero"
assert template.render(n=-1) == "negative"
```

Empty block bodies are allowed. Unexpected branches, mismatched terminators,
unclosed tags, and unsupported syntax raise `SyntaxError` with the source
line and column of the relevant tag.

## Assignments

`{% set name = expression %}` assigns a value in the current render. Values
assigned in a loop remain available after it. Each render starts with fresh
variable bindings. Context containers are checked without copying, at the
moment the template first reads them; assignments rebind names only within the
render. Trusted filters receive the
original containers and are responsible for any mutations they perform. Tuple
unpacking is supported. Assign to names, not attributes or indexed elements:

<!-- name: test_assignments -->
```python
from rmote.templates import Template

template = Template(
    "{% set total = 0 %}"
    "{% for n in numbers %}{% set total = total + n %}{% endfor %}"
    "total={{ total }}"
)
assert template.render(numbers=[1, 2, 3]) == "total=6"
assert template.render(numbers=[]) == "total=0"
```

Loops and branches share the render's bindings. A loop target keeps its last
assigned value after a nonempty loop; an empty loop does not assign it. Prefer
distinct loop variable names when nesting loops. `set` cannot modify
`settings.port` or `items[0]`; construct the data in Python or return a new value
from a filter.

## Whitespace and Comments

Text between tags is preserved exactly, including indentation, blank lines,
line endings, and a final newline. Tags add no whitespace themselves. A tag
on its own line therefore leaves that line's newline in the output.

A minus immediately inside a delimiter removes adjacent **source** whitespace:

- `{%-` and `-%}` trim before or after a command.
- `{{-` and `-}}` trim before or after an expression.
- `{#-` and `-#}` trim before or after a comment.

Trimming includes spaces, tabs, and newlines; it does not strip characters
produced by an expression. Use a space in `{{ -2 }}` when the minus is part
of a negative value rather than a trimming marker.

<!-- name: test_whitespace -->
```python
from rmote.templates import Template

assert Template("a\n{% if False %}hidden{% endif %}\nb\n").render() == "a\n\nb\n"
assert Template("a \n {{- value -}} \n b").render(value=42) == "a42b"

template = Template("""\
{% for port in ports -%}
listen {{ port }}{% if port == 443 %} ssl{% endif %};
{% endfor -%}
""")
assert template.render(ports=[80, 443]) == "listen 80;\nlisten 443 ssl;\n"
```

`{# … #}` comments can span lines and contain other template delimiters.
They emit no text, but surrounding whitespace is preserved unless trimmed:

<!-- name: test_comments -->
```python
from rmote.templates import Template

assert Template("a{# ignored {{ missing }} #}b").render() == "ab"
assert Template("{# configuration -#}\nport={{ port }}\n").render(port=8080) == "port=8080\n"
```

A backslash before `{{`, `{%`, or `{#` emits that opening delimiter literally.
Alternatively, insert the delimiter using a string expression:

<!-- name: test_escape -->
```python
from rmote.templates import Template

assert Template(r"\{{ missing }}").render() == "{{ missing }}"
assert Template(r"\{% if missing %}").render() == "{% if missing %}"
assert Template("{{ '{{' }}").render() == "{{"
```

## Compilation and intermediate representation

`Template.__init__` compiles the source into an immutable `TemplateProgram`:

1. `TemplateLexer` produces `TextToken`, `ExpressionToken`, `CommandToken`, and
   `CommentToken` objects with source offsets. Each code tag is tokenized once;
   expression tokens retain their code lexemes, and command tokens retain the
   command name and argument lexemes.
2. `TemplateParser` and `ExpressionParser` build typed expression and instruction
   nodes from those lexemes, without tokenizing again. Attribute access uses
   `AttributeExpression`; indexing uses `LookupExpression`. Other nodes include
   `FilterExpression`, `TextNode`, `OutputNode`, `IfNode`, `ForNode`, and `SetNode`.
3. `render()` invokes those nodes with a fresh `RenderContext`. It does not
   tokenize, parse, generate Python, or call `eval`/`exec`.

`template.program` exposes the compiled representation. Compilation is cached
by source and filter classes. The parser, program, and interpreter are separate
components; the current implementation provides only the restricted language.

Pickle transfers the program and filter mapping. Restoring a `Template`,
including on remote hosts, does not parse or recompile its source:

<!-- name: test_pickling -->
```python
import pickle
from rmote.templates import Template

restored = pickle.loads(pickle.dumps(Template("port={{ port }}")))
assert restored.render(port=8080) == "port=8080"
```

For example, render an nginx configuration locally before sending it to a host:

<!-- name: test_nginx_vhost -->
```python
from rmote.templates import Template

vhost = Template("""\
{# nginx vhost -#}
server {
    listen {{ port }};
    server_name {{ hostname }};

    location / {
        proxy_pass http://127.0.0.1:{{ backend_port }};
    }
}
""")

rendered = vhost.render(port=8080, hostname="example.com", backend_port=9000)
assert "server_name example.com;" in rendered
assert "proxy_pass http://127.0.0.1:9000;" in rendered
assert rendered.startswith("server {")
assert rendered.endswith("}\n")
```

## The `render_template` Helper

{func}`~rmote.templates.render_template` compiles and renders in one step:

<!-- name: test_render_template -->
```python
from rmote.templates import render_template

source = "Hi {{ name }}, you have {{ count }} message{{ 's' if count != 1 else '' }}."
assert render_template(source, name="Bob", count=3) == "Hi Bob, you have 3 messages."
assert render_template(source, name="Alice", count=1) == "Hi Alice, you have 1 message."
```

This helper uses the built-in filter mapping. To register custom filters,
construct `Template(source, filters=...)` and call its `render` method.

## The `RenderTemplate` Tool

{class}`~rmote.tools.template.RenderTemplate` renders on the **remote** side.

| Method | Input | Use when |
|---|---|---|
| `render(template, **kw)` | Source string or `Template` instance | Compile on the target or reuse a compiled template |
| `render_file(path, **kw)` | String or `Path` on the remote filesystem | The template lives on the target |

Call these methods through either client. This example renders in a separate
Python process; use `Connection.from_ssh` to render on an SSH host:

<!-- name: test_remote_template; fixtures: client_resources; mark: timeout(15) -->

```python
from rmote.templates import Template
from rmote.sync import Connection
from rmote.tools import RenderTemplate

compiled = Template("hosts={{ hosts | unique | sort | join(', ') }}")
with Connection.from_local() as remote:
    rendered = remote(RenderTemplate.render, "port={{ port }}", port=8080)
    assert rendered == "port=8080"
    assert remote(RenderTemplate.render, compiled, hosts=["web", "db", "web"]) == "hosts=db, web"
```

See {doc}`api/tools/template` for the method reference.

Rendering returns text; it does not write a configuration file. Pass the result
to a file operation when it is ready. A relative path passed to `render_file`
is resolved in the remote process's working directory.

Source strings and `render_file` use the built-in filters. For custom filters,
read any local source file yourself, construct a `Template` with `filters=...`,
and send that object to `RenderTemplate.render`. Passing `filters=` as a render
keyword supplies a context variable; it does not register filters. See
{doc}`template-filters` for both synchronous and asynchronous remote examples.

## Diagnose template errors

Construct `Template` during setup to catch syntax errors before connecting to a
host. Syntax errors are subclasses of `SyntaxError`, with the template text and
the line and column of the relevant tag. Rendering then checks the actual values
and calls the selected filters.

| When | Common error | What to check |
|---|---|---|
| Construction | `SyntaxError` | Unclosed or mismatched tags, unsupported expressions, unknown filters |
| Filter registration | `ValueError` or `TypeError` | Public identifier names and `TemplateFilter` classes, not instances |
| Context validation | `TypeError` or `ValueError` | Application objects, generators, custom container subclasses, cycles |
| Rendering | `NameError`, `KeyError`, `IndexError`, `AttributeError` | Missing variables or lookups; use `default` for optional data |
| Filter invocation | `TypeError` or the filter's exception | Argument names and types, input validation, dependencies |
| Remote transfer | `TypeError` about unavailable class source | Define custom filters in readable `.py` files |

<!-- name: test_template_error_location -->
```python
from rmote.templates import Template

try:
    Template("header\n{{ value | unknown_filter }}")
except SyntaxError as error:
    assert error.lineno == 2
    assert error.offset is not None
    assert "unknown_filter" in str(error)
else:
    raise AssertionError("An unknown filter must fail during compilation")
```

The language implements the features described here. It has no template
inheritance, `include`, macros, implicit `loop` metadata, or Jinja tests such as
`is defined`. It also forbids calls such as `range(3)` and `data.items()` inside
expressions. Prepare those values in Python or provide a registered filter.
Use `is none` to check an existing value against `None`, and `default` to handle
an absent value. The syntax resembles Jinja2, but existing Jinja2 templates may
need changes before they can be used here.

## Measuring performance

From the repository root, run the fixed workloads with an optional Jinja2
reference:

```console
uv run --with jinja2 python -m benchmarks.template
uv run python -m benchmarks.template --profile loop_if_filters
```

The script checks matching output and prints a Markdown table with medians of
seven batches. Compilation clears the cache on each iteration; rendering reuses
the program and includes context validation. The workloads cover scalars, loops,
conditions with filters, filter chains, nested lookups, a large context, a loop
of the size a real configuration file has, and a context of many names.
Jinja2 is optional and is not a package dependency. Profile results are sorted
by internal time. Use `--help` to adjust the number of samples and iterations.

Measured on a quiet machine with Python 3.14.2 and Jinja2 3.1.6, a frozen
snapshot renders every loop workload at the speed of Jinja2 or faster:
`loop_1000` 0.78 ms (0.91× of Jinja2), `deep_lookup` 0.81 ms (1.00×),
`loop_if_200` 0.147 ms (1.02×), and `wide_context` 2 µs against 32 µs. The same
workloads on a plain context cost 1.9 to 2.5 times what Jinja2 costs, because
every value the template reads is checked once per render.
