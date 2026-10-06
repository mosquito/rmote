# Python REPL

`rmote repl` opens a Python console with an established connection named
`remote`. Calls are synchronous by default. Use `-a` / `--async` for a `Protocol`
and top-level `await`, like `python -m asyncio`.

## Connect

Choose any of rmote's common {doc}`transports`; the console interface stays the same.

```bash
rmote repl                                      # local Python subprocess
rmote repl -- ssh -T server
rmote repl -- docker exec -i my-container
rmote repl -- kubectl exec -i pod/my-pod --
rmote repl --async -- ssh -T server
```

`python -m rmote repl` accepts the same options. Put rmote options before
`--`, followed by the transport command. rmote appends `python3 -qui`;
override the interpreter with `-p` / `--python /path/to/python`. Without a transport,
the default is the interpreter running rmote. The target needs Python 3.11+
but no rmote installation.

Use `-v` / `--debug` for protocol logs and `-h` / `--help` for all options.

For Docker use `exec -i`, without `-t`. For SSH use `-T`. The transport carries
the binary protocol; the Python console and its terminal live locally.

## Inspect the host

At startup rmote collects `system` and `python` facts into `host`, an ordinary
dictionary whose values are typed fact models. The banner shows the remote
hostname, operating system, architecture and Python version.

```text
>>> host["system"].hostname
'my-container'
>>> host["python"].version
'3.14.2'
>>> more = remote(facts.gather, sections=["cpu", "memory"])
>>> host.update(more)
>>> result = remote(Exec.command, "uname", "-a", capture_output=True)
>>> print(result.stdout.decode(), end="")
```

With `--async`, use `await` for remote calls:

```text
>>> more = await remote(facts.gather, sections=["cpu", "memory"])
>>> host.update(more)
>>> result = await remote(Exec.command, "uname", "-a", capture_output=True)
```

`host` is a startup snapshot, not an automatically refreshed cache. Request
more branches or refresh it with `facts.gather` whenever needed. See
{doc}`api/tools/facts` for available collectors and models.

The namespace contains `rmote`, `asyncio`, `remote`, `host`, `Tool`, `inline`,
`Protocol`, `Connection`, `process`, and every public name from `rmote.tools`,
including `facts`, `FileSystem` and `Exec`. Python expressions run locally;
only calls through `remote` execute on the connected host.

## Define a tool

Enter a blank line after a class or function to finish its definition:

```text
>>> class Identity(Tool):
...     @staticmethod
...     def pid():
...         import os
...         return os.getpid()
...
>>> remote(Identity.pid)
1234
```

In async mode the call is `await remote(Identity.pid)`. As with other inline
tools, keep imports and dependencies inside the class or its methods; local
REPL variables are not a remote namespace. Imported tools from ordinary files
also work. See {doc}`writing-tools` for the transfer rules.

The console retains entered source in memory so that `Tool` can retrieve it
with `inspect`. Its `__main__.__file__` is a virtual name, `<rmote-repl>`,
not a file on disk.

## Scripts and exit

Use `-c` for a single command, or pipe a Python script into the console:

```bash
rmote repl -c 'print(host["system"].hostname)' -- ssh -T server
rmote repl --async -c 'print(await remote(facts.gather, sections=["python"]))' -- ssh -T server
printf 'print(host["system"])\n' | rmote repl -- docker exec -i my-container
```

Script mode prints no banner or prompts. An uncaught exception returns status
1; `exit(n)` uses status `n`. Interactive errors return to the prompt.
Readline editing, completion and session history are available when Python
includes readline.

Ctrl-C interrupts the current local call and returns to the prompt. It does
not guarantee cancellation of an operation already running remotely.
Ctrl-D or `exit()` closes the connection and its transport process. A lost
transport requires a new session. In async mode the same event loop stays
alive while waiting for input, so background tasks and RPC continue running.
