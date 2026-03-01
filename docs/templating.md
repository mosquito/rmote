# Templating

## Motivation

rmote's core constraint is **zero remote dependencies** - only the Python stdlib is available on
the other side.  This rules out Jinja2, Mako, and every other templating library you might reach
for on a normal project.

The built-in engine is intentionally minimal.  It covers exactly what is needed to generate config
files and scripts during remote bootstrapping: variable interpolation and basic Python control flow.
Nothing more.  If your use-case calls for filters, template inheritance, macros, or auto-escaping,
use a real templating library locally and pass the already-rendered string to the remote side.

The engine lives entirely inside `protocol.py`, which is the compressed payload injected into the
remote interpreter.  This means `Template` instances are available on both sides without any extra
sync step and are picklable, so they can be passed as arguments to remote tool calls directly.

## Template Syntax

### Variable Interpolation

Wrap any Python expression in `${…}` to insert its string representation.

<!-- name: test_interpolation -->
```python
from rmote.protocol import Template

assert Template("Hello, ${name}!").render(name="Alice") == "Hello, Alice!"
```

Any Python expression works, including calls and comprehensions.  Nested braces
are handled correctly so dict literals and method calls with keyword arguments
are fine:

<!-- name: test_interpolation -->
```python
tmpl = Template("Keys: ${', '.join(sorted(d.keys()))}")
assert tmpl.render(d={"b": 2, "a": 1}) == "Keys: a, b"
```

To emit a literal `${` without triggering interpolation, escape the dollar sign
with a backslash:

<!-- name: test_escape -->
```python
from rmote.protocol import Template

assert Template(r"\${not_a_var}").render() == "${not_a_var}"
```

### Control-Flow Lines

Lines whose first non-whitespace character is `%` introduce a Python
control-flow statement.  Indentation is managed automatically; explicit
end-markers close the block:

<!-- name: test_for_loop -->
```python
from rmote.protocol import Template

tmpl = Template("""\
% for item in items:
- ${item}
% endfor""")

assert tmpl.render(items=["alpha", "beta", "gamma"]) == "- alpha\n- beta\n- gamma"
```

Continuation keywords (`else`, `elif`, `except`, `finally`) adjust the indent
level automatically:

<!-- name: test_if_elif_else -->
```python
from rmote.protocol import Template

tmpl = Template("""\
% if n > 0:
positive
% elif n == 0:
zero
% else:
negative
% endif""")

assert tmpl.render(n=1) == "positive"
assert tmpl.render(n=0) == "zero"
assert tmpl.render(n=-1) == "negative"
```

A bare `% end` closes any open block when you prefer a generic terminator:

<!-- name: test_bare_end -->
```python
from rmote.protocol import Template

tmpl = Template("""\
% for x in xs:
${x}
% end""")

assert tmpl.render(xs=[1, 2]) == "1\n2"
```

### Literal `%`

Double the percent sign at the start of a line to emit a literal `%`.
`${…}` expressions are still expanded on `%%` lines:

<!-- name: test_literal_percent -->
```python
from rmote.protocol import Template

tmpl = Template("""\
%% done ${n}/10""")

assert tmpl.render(n=7) == "% done 7/10"
```

### Comments

Lines starting with `##` are stripped from the output entirely:

<!-- name: test_comments -->
```python
from rmote.protocol import Template

tmpl = Template("""\
## this line is ignored
result: ${value}""")

assert tmpl.render(value=42) == "result: 42"
```

## The `Template` Class

`Template` compiles the template string once and caches the render function.
Repeated calls with the same template string are free - the compiled function
is reused.

`Template` instances are **picklable**.  They store only the original template
string, so they can be passed directly as arguments to remote tool calls over
the protocol without recompiling on the remote side.

<!-- name: test_pickling -->
```python
import pickle
from rmote.protocol import Template

tmpl = Template("port=${port}")
data = pickle.dumps(tmpl)
restored = pickle.loads(data)

assert restored.render(port=8080) == "port=8080"
```

A practical use-case is generating config files.  The template is compiled
locally, pickled, sent to the remote process, and rendered there with
host-specific variables - all without shipping Jinja2 or Mako to the remote:

<!-- name: test_nginx_vhost -->
```python
from rmote.protocol import Template

vhost = Template("""\
## nginx vhost
server {
    listen ${port};
    server_name ${hostname};

    location / {
        proxy_pass http://127.0.0.1:${backend_port};
    }
}""")

rendered = vhost.render(port=443, hostname="example.com", backend_port=8080)
assert "server_name example.com;" in rendered
assert "proxy_pass http://127.0.0.1:8080;" in rendered
assert "## nginx vhost" not in rendered
```

## The `render_template` Helper

{func}`~rmote.protocol.render_template` compiles and renders in one step:

<!-- name: test_render_template -->
```python
from rmote.protocol import render_template

result = render_template(
    "Hi ${name}, you have ${count} message${'s' if count != 1 else ''}.",
    name="Bob",
    count=3,
)
assert result == "Hi Bob, you have 3 messages."

singular = render_template(
    "Hi ${name}, you have ${count} message${'s' if count != 1 else ''}.",
    name="Alice",
    count=1,
)
assert singular == "Hi Alice, you have 1 message."
```

## The `Template` Tool

{class}`~rmote.tools.template.Template` is a built-in {class}`~rmote.protocol.Tool`
that renders templates on the **remote** side.  Its three methods mirror the
three ways to supply a template:

| Method                        | Input               | Use when                          |
|-------------------------------|---------------------|-----------------------------------|
| `render(template, **kw)`      | template string     | template is short / dynamic       |
| `render_file(path, **kw)`     | path on remote FS   | template lives on the remote host |
| `render_compiled(tmpl, **kw)` | `Template` instance | template was compiled locally     |

The methods execute on the remote process - call them through a `Protocol`
instance as with any other tool.  See {doc}`api/tools/template` for the full
method reference.
