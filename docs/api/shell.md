# Shell Client

The local terminal client behind the `rmote shell` command. It bridges
the local standard descriptors to a session that the {doc}`Vty tool <tools/vty>`
starts on the remote host. See the [interactive shell guide](../shell.md) for
the command line.

```{eval-rst}
.. autofunction:: rmote.cli.shell.run_shell
```

```{eval-rst}
.. autofunction:: rmote.cli.shell.run
```

```{eval-rst}
.. autofunction:: rmote.cli.shell.configure_parser
```

## Local terminal handling

```{eval-rst}
.. autoclass:: rmote.cli.shell.TerminalMode
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.cli.shell.NonBlocking
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autofunction:: rmote.cli.shell.guard_terminal
```

```{eval-rst}
.. autoclass:: rmote.cli.shell.EscapeFilter
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.cli.shell.ShellSession
   :members:
   :show-inheritance:
```

## Helpers

```{eval-rst}
.. autofunction:: rmote.cli.shell.terminal_size
```

```{eval-rst}
.. autofunction:: rmote.cli.shell.regular_file
```

```{eval-rst}
.. autofunction:: rmote.cli.shell.read_available
```
