# Platform requirements

Import the checks from `rmote.requires`. A tool that reads a kernel interface
of Linux, or runs the program of one distribution, calls one of them before it
touches that interface. The refusal is `NotImplementedError`, and its message
names the operation, what it needs and what the host is:

```text
Sysctl.get needs the /proc/sys interface of Linux, and this host runs Darwin
Service needs the systemctl program of systemd, and this host has none
AptRepository.absent needs /etc/apt of Debian and Ubuntu, and this host has none
```

The reason has to be in the message, because the failure travels through RPC
and the caller sees the exception and nothing else. A missing file or a missing
program on its own reads like a damaged host instead of the wrong one. See
{doc}`tools/index` for what each built-in tool needs.

A tool of your own uses the same checks. The module transfers on demand, like
every other module a tool imports.

```{eval-rst}
.. autofunction:: rmote.requires.needs_linux

.. autofunction:: rmote.requires.needs_program

.. autofunction:: rmote.requires.needs_file
```
