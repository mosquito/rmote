# Service

Manage systemd units on the target. The target must run systemd, and the
connection user must have permission to manage the requested service.
See {doc}`../../quickstart` for rendering a unit and checking a running service.

```{eval-rst}
.. autoclass:: rmote.tools.service.Service
   :members:
   :show-inheritance:
```

## Types

```{eval-rst}
.. autoclass:: rmote.tools.service.State
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.tools.service.Result
   :members:
   :show-inheritance:
```
