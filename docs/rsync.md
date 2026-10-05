# File and directory synchronization

`rmote rsync` synchronizes files and directory contents with hosts and
containers that have **only Python 3.11+ and its standard library** available.
Install rmote on the machine where you run the command. The remote side needs
no `rsync` executable, rmote installation, third-party Python packages or
separately installed agent.

This is useful for copying a project into a minimal Python container,
deploying files to a host, or downloading results without first provisioning
transfer software on each destination. The chosen transport starts a remote
Python process with stdin and stdout connected to rmote. rmote then
bootstraps the connection and sends the Python code needed to compare and
copy files. With `docker exec -i`, this also works without SSH or an SSH
server inside the container.

The command exposes the {doc}`Rsync API <api/tools/rsync>` for directories
and {doc}`FileSync <api/tools/file_sync>` for individual files. They compare
file contents and reuse matching blocks already at the destination, so repeat
transfers send changed content. The CLI manages the connection, reports
transfers and deletions, and prints totals; Python automation can use the
same tools through an existing rmote connection.

Mark exactly one endpoint with `remote:`. The `-r` / `--exec` option selects
the command that reaches that host:

```bash
# Upload directory contents.
rmote rsync -r 'ssh -T server' ./project remote:/srv/project

# Download them.
rmote rsync --exec 'ssh -T server' remote:/srv/project ./copy

# Use a running container.
rmote rsync -r 'docker exec -i my-container' ./project remote:/app

# Copy an individual file, in either direction.
rmote rsync -r 'ssh -T server' ./config.ini remote:/etc/app/config.ini
rmote rsync -r 'ssh -T server' remote:/var/log/app.log ./app.log
```

`python -m rmote rsync` is equivalent. rmote appends `python3 -qui` to
the transport. Use `-p` / `--python /path/to/python` to select its interpreter.
Omitting `--exec` starts a subprocess with the current local Python; the
`remote:` marker still selects which side uses that subprocess.

The command string supports shell-style quotes, including quoted paths with
spaces. It is split into arguments and executed directly; pipes, redirects,
variable expansion and other shell expressions are not interpreted. To use a
shell, name it explicitly in the transport command.

Use SSH `-T` and Docker `exec -i` without `-t`: the transport must preserve
stdin and stdout bytes. `remote:` is a side marker, not a hostname; the host
is chosen entirely by `--exec`. Relative paths use each side's working
directory. Use `--` before positional paths beginning with a dash.

## Directory and file behavior

Directories are always recursive; `-r` means the transport command, not a
recursion switch. Directory contents go directly into the destination:
`./project remote:/srv/project` copies `project/file` to `/srv/project/file`.
A trailing slash does not change that rule.

The destination's parent must exist; a missing destination directory is
created. Empty directories and symbolic links inside the tree are copied,
without following links. Directory roots and their parents must be real
directories, not symbolic links. Special files and conflicts between file,
directory and link types are rejected.

For an individual regular file, an existing destination directory receives
the source basename; otherwise the destination is the exact output filename.
The source cannot be a standalone symlink. Each changed file is replaced
atomically. The whole tree is not a transaction: completed transfers remain
if a later file fails.

## Exclusions and deletion

Extra destination entries are kept by default. Enable deletion with `-d` / `--delete`:

```bash
rmote rsync -r 'ssh -T server' \
  --exclude '.git/' '__pycache__/' '*.pyc' \
  --delete ./project remote:/srv/project
```

Exclusions apply on both sides. Excluded destination entries and their parent
directories survive `--delete`. Deletion runs only after all transfers
succeed. `-x` is a shortcut for `--exclude`. Matching
rules are described in {doc}`api/tools/rsync`.

To remove previously copied files that now match an exclusion, use
`-D` / `--delete-excluded`. It implies `--delete`, removing both extra and excluded
destination entries. Excluded source files remain untouched and are not copied:

```bash
rmote rsync -x .venv .idea '.*cache' '*.pyc' -D \
  -r 'docker exec -i my-container' . remote:/opt/project
```

`--exclude`, `--delete` and `--delete-excluded` require a directory source.
Each `-x` / `--exclude` accepts zero or more patterns, and repeated options combine
their patterns. Another option, such as `--delete` or `-r`, ends the list.
If the source and destination follow it directly, separate them with `--`:

```bash
rmote rsync -r 'ssh -T server' --exclude '.venv' '.idea' '*.pyc' -- ./project remote:/srv/project
```

## Logs and options

The default stderr log reports each changed file's transferred and reused
content bytes in one line per file, plus created directories, updated symlinks
and deletions. Metadata details are included only with `--debug`.
A final line shows totals, elapsed time and transfer speed:

```text
file 'hello.txt': 5 bytes transferred, 0 reused
Updated: 1 files checked, 5 bytes transferred, 0 bytes reused; ...
```

Fully reused files have no individual log line. An identical repeat reports
`Up to date`, zero transferred bytes and the total number of reused bytes.
Counts describe file contents, not protocol overhead
or compressed bytes on the wire. Stdout remains empty. Use `-q` / `--quiet`
for errors only, or `-v` / `--debug` for protocol logs and tracebacks.

`-j N` / `--concurrency N` controls simultaneous file transfers (default 32,
also in the Python API).
`-B BYTES` / `--block-size BYTES` controls comparison blocks (default 4 MiB, maximum
16 MiB). Matching blocks are reused, even when timestamps cannot be trusted.

Source permission bits are preserved by default. `-P` / `--no-preserve-mode` keeps
existing destination modes; new files use 0600 and new directories 0700.
Owners and groups are changed only with explicit `-o` / `--owner NAME` or
`-g` / `--group NAME`, resolved on the destination host. Timestamps, ACLs, xattrs
and hard-link identity are not copied.

Success returns status 0, invalid CLI arguments 2, transfer failures 1 and
Ctrl-C 130. An error does not print a success summary. The implementation uses
{doc}`api/tools/rsync` for trees and {doc}`api/tools/file_sync` for individual
files; their filesystem stability and cancellation rules also apply.
