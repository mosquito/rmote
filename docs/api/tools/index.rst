Built-in Tools
==============

Pass a tool method and its arguments to ``Connection`` or ``Protocol``.
Methods execute on the target with that connection's user permissions. System
commands used by a tool must be installed there. See :doc:`../../quickstart`
for user setup, package installation, and service management.

.. automodule:: rmote.tools

.. toctree::
   :maxdepth: 1

   filesystem
   exec
   service
   user
   apt
   apt_repository
   pacman
   pacman_repository
   logger
   quit
   template
