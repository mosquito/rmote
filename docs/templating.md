# Templating

Use `Template` to render configuration text with Python expressions and control
flow. It is available locally and in remote tools without an extra package.
The {doc}`quickstart` uses it to create a systemd service unit.

Render locally when all values are known to the client. Render in a remote
tool when values come from that host. You can also use another template engine
locally and send the resulting string to the target.

Templates execute Python expressions and statements. Treat their source as
trusted code. Values are not escaped for shells, HTML, or configuration formats;
validate or escape values for the format you generate.

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

`Template` caches compiled render functions by source text within each process.
A matching cache entry avoids compilation; every `render()` call still executes
the function with the supplied values.

Pickling stores the template source, not the compiled function. Unpickling
constructs a `Template` in the receiving process. That process compiles the
source unless it already has a matching cache entry.

<!-- name: test_pickling -->
```python
import pickle
from rmote.protocol import Template

tmpl = Template("port=${port}")
data = pickle.dumps(tmpl)
restored = pickle.loads(data)

assert restored.render(port=8080) == "port=8080"
```

For example, render an nginx configuration locally before sending it to a host:

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

rendered = vhost.render(port=8080, hostname="example.com", backend_port=9000)
assert "server_name example.com;" in rendered
assert "proxy_pass http://127.0.0.1:9000;" in rendered
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
| `render_compiled(tmpl, **kw)` | `Template` instance | reuse a template object           |

Call these methods through either client. This example renders in a separate
Python process; use `Connection.from_ssh` to render on an SSH host:

<!-- name: test_remote_template; fixtures: client_resources; mark: timeout(15) -->
```python
from rmote.sync import Connection
from rmote.tools import Template as RemoteTemplate

with Connection.from_local() as remote:
    rendered = remote(RemoteTemplate.render, "port=${port}", port=8080)
    assert rendered == "port=8080"
```

See {doc}`api/tools/template` for the method reference.
