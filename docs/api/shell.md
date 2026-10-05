# Shell Client

The local terminal client behind the `rmote-shell` console script. It bridges
the local standard descriptors to a session that the {doc}`Vty tool <tools/vty>`
starts on the remote host. See the [interactive shell guide](../shell.md) for
the command line.

```{eval-rst}
.. autofunction:: rmote.shell.run_shell
```

```{eval-rst}
.. autofunction:: rmote.shell.main
```

```{eval-rst}
.. autofunction:: rmote.shell.build_parser
```

## Local terminal handling

```{eval-rst}
.. autoclass:: rmote.shell.TerminalMode
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.shell.NonBlocking
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autofunction:: rmote.shell.guard_terminal
```

```{eval-rst}
.. autoclass:: rmote.shell.EscapeFilter
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.shell.ShellSession
   :members:
   :show-inheritance:
```

## Helpers

```{eval-rst}
.. autofunction:: rmote.shell.terminal_size
```

```{eval-rst}
.. autofunction:: rmote.shell.regular_file
```

```{eval-rst}
.. autofunction:: rmote.shell.read_available
```
