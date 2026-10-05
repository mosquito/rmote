# Terminal Sessions

`Vty` runs a command on the remote host and streams its output. A session with a
terminal gives job control, a window size and full screen programs. A session
with pipes keeps the bytes exactly and reports a real end of input.

The module needs a POSIX host, because a terminal comes from `pty` and
`termios`.

The output call is a streaming call, so the bytes arrive as the child writes
them. See [interactive shell](../../shell.md) for the command line client built
on this tool.

```{eval-rst}
.. autoclass:: rmote.tools.vty.Vty
   :members:
   :show-inheritance:
```

## Session backends

The tool chooses a backend from the `want_pty` argument. Application code uses
`Vty` and does not touch these classes, but their contracts explain how the end
of input and the cleanup behave.

```{eval-rst}
.. autoclass:: rmote.tools.vty.Session
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.tools.vty.TerminalSession
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.tools.vty.PipeSession
   :members:
   :show-inheritance:
```

## Controlling terminal

A child needs a controlling terminal, or a shell reports that job control is
off. The helper below takes it after exec, in a fresh interpreter with one
thread. A `preexec_fn` would instead run Python between fork and exec in a
process that has threads, where the standard library warns about a deadlock.

```{eval-rst}
.. autofunction:: rmote.tools.vty.take_controlling_terminal
```

```{eval-rst}
.. autofunction:: rmote.tools.vty.launcher_source
```

## Helpers

```{eval-rst}
.. autofunction:: rmote.tools.vty.default_shell
```

```{eval-rst}
.. autofunction:: rmote.tools.vty.set_winsize
```

```{eval-rst}
.. autofunction:: rmote.tools.vty.wait_readable
```

```{eval-rst}
.. autofunction:: rmote.tools.vty.wait_writable
```

```{eval-rst}
.. autofunction:: rmote.tools.vty.write_all
```
