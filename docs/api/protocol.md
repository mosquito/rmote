# Protocol and Tool Support

## Asynchronous connections

Use `Protocol` from an asynchronous application. Enter its async context
before calling tools. See {doc}`../quickstart` for a complete connection example
and {doc}`../concepts` for subprocess ownership and cancellation.

```{eval-rst}
.. autoclass:: rmote.protocol.Protocol
   :members:
   :show-inheritance:
   :special-members: __call__, __aenter__, __aexit__
```

## Tool definitions and commands

Subclass `Tool` in a readable Python module. Use `process` inside a tool to
run commands; set `capture_output=True` when the return value needs output.
See {doc}`../writing-tools` for executable examples.

```{eval-rst}
.. autoclass:: rmote.protocol.Tool
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autofunction:: rmote.protocol.process
```

## Templates

These helpers render in the process where they are called. To render on a
connected host, use {doc}`tools/template`. See {doc}`../templating` for syntax.

```{eval-rst}
.. autoclass:: rmote.protocol.Template
   :members:
   :show-inheritance:
   :special-members: __init__, __reduce__
```

```{eval-rst}
.. autofunction:: rmote.protocol.render_template
```

## Protocol internals

The clients use these helpers for bootstrap, source transfer, and packet
handling. Application code normally calls tools through a connection.

```{eval-rst}
.. autoclass:: rmote.protocol.BaseProtocol
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.protocol.Flags
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autofunction:: rmote.protocol.tool_to_dict
```

```{eval-rst}
.. autofunction:: rmote.protocol.tool_from_dict
```

```{eval-rst}
.. autofunction:: rmote.protocol.bootstrap_packer
```
