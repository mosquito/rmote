# Quickstart

Use rmote to install and configure software on another machine from Python.
Start by creating an administrator account, installing its SSH key, and granting
sudo access. Then deploy a Redis cache: install its package, render a systemd
service, start it, and check the result.

For convenience, Docker supplies the target machine. The same deployment works
over SSH: only the connector changes. You install rmote locally; the target
needs Python, but does not need rmote or an agent. See {doc}`transports` for
the shared connection model and other ways to reach Python.

## Install rmote

Use Python 3.11 or newer on your local machine:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install rmote
```

Keep this environment active when you run the Python examples below.

## Prepare a Disposable Host

The example needs a Debian or Ubuntu host with Python and systemd.
We will create one in Docker so you can try package installation and service
management without changing your own machine.

Save this as `Dockerfile` in a new directory:

```{literalinclude} ../examples/quickstart/Dockerfile
:language: dockerfile
```

Build the image and start the container:

```bash
docker build -t rmote-quickstart .
docker run -d --rm --name rmote-quickstart \
    --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock \
    rmote-quickstart
```

Here systemd runs as PID 1. The privileged container is a disposable test host
for this guide; ordinary application containers do not need this setup.
It publishes no ports and mounts no host directories.
Use a Docker engine that permits privileged containers and private cgroup
namespaces. A rootless engine may not support this setup.

## Connect and Run a Command

Create `deploy.py`. Start with this connector:

<!-- name: test_quickstart; case: connect; fixtures: quickstart_container; mark: docker, timeout(300) -->
```python
import asyncio
import os
from contextlib import asynccontextmanager

from rmote.protocol import Protocol
from rmote.tools import Apt, Exec, FileSystem, Service, User


@asynccontextmanager
async def connect():
    container = os.environ.get("RMOTE_CONTAINER", "rmote-quickstart")
    process = await asyncio.create_subprocess_exec(
        "docker", "exec", "-i", container, "python3", "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        async with await Protocol.from_subprocess(process) as remote:
            yield remote
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        await process.wait()
```

Set `RMOTE_CONTAINER` if you used a different container name.
The connector starts `python3 -qui` inside the container (using standard Python
flags: quiet `-q`, unbuffered `-u`, and interactive `-i` to read statements from stdin)
and lets rmote communicate through its standard input and output. It also closes
the process after use. The later SSH connector manages its own process and is shorter.

Append your first remote operation to `deploy.py`:

<!-- name: test_quickstart; case: command -->
```python
async def show_host():
    async with connect() as remote:
        result = await remote(Exec.command, "uname", "-s", capture_output=True)
        assert result.stdout.strip() == b"Linux"
        print(result.stdout.decode(), end="")


asyncio.run(show_host())
```

Run `python deploy.py`. It should print `Linux`, even if your local machine
runs macOS. The command executes inside the container.

`Exec` is a built-in **tool**: a group of functions that run on the target.
Pass the function itself to `remote`, followed by its arguments.
`Exec.command` returns a result with `stdout`, `stderr`, and `returncode`.
Pass `capture_output=True` when you need output. Otherwise, both streams are
discarded on the target, and `stdout` and `stderr` are `None`. Captured output
is bytes; `.decode()` converts it to text.
A failed command raises `subprocess.CalledProcessError` unless you pass
`check=False`.

## Create an Administrator

Start with the account that will maintain this host. These operations need
root, which is the default user of our Docker container. On an existing SSH
host, use an account that can already run the required operations as root.

Create a key for this disposable example on your local machine:

```bash
ssh-keygen -t ed25519 -N '' -f ./quickstart_key
```

Use a new filename if `quickstart_key` already exists. Only its `.pub` file is
sent to the target. Set `RMOTE_SSH_PUBLIC_KEY` to use another public key.
For a real administrator account, use the public key you intend to authorize.

Append this step to `deploy.py`:

<!-- name: test_quickstart; case: administrator -->
```python
from pathlib import Path

ADMIN = "deploy"
public_key = Path(os.environ.get("RMOTE_SSH_PUBLIC_KEY", "quickstart_key.pub")).read_text().strip()


async def prepare_administrator():
    async with connect() as remote:
        user = await remote(User.present, ADMIN, create_home=True, shell="/bin/bash")
        assert user.name == ADMIN and user.home == f"/home/{ADMIN}"
        assert user.uid != 0
        assert not (await remote(User.present, ADMIN, create_home=True, shell="/bin/bash")).changed

        await remote(User.authorized_key, ADMIN, public_key)
        keys = await remote(FileSystem.read_str, f"{user.home}/.ssh/authorized_keys")
        assert public_key in keys.splitlines()
        assert not await remote(User.authorized_key, ADMIN, public_key)
        print(f"{user.name}: uid={user.uid}, home={user.home}")


asyncio.run(prepare_administrator())
```

Run `python deploy.py`. `User.present` creates the user and home directory if
needed. `User.authorized_key` adds the public key while retaining existing keys.
Repeating these calls with unchanged settings makes no further changes.

The container uses `docker exec`, so this step does not require an SSH server.
On an SSH host, the key permits login only if its SSH server allows this user
and public-key authentication. Creating the account does not configure sshd.

## Grant and Check sudo Access

Install `sudo`, then create and check its rule:

<!-- name: test_quickstart; case: sudo -->
```python
async def configure_sudo():
    async with connect() as remote:
        await remote(Apt.update, ttl=3600)
        package = await remote(Apt.package, "sudo")
        assert package.version
        await remote(User.sudoer, ADMIN, nopasswd=True)
        assert not await remote(User.sudoer, ADMIN, nopasswd=True)

        rule = await remote(FileSystem.read_str, f"/etc/sudoers.d/{ADMIN}")
        assert rule == f"{ADMIN} ALL=(ALL) NOPASSWD: ALL\n"
        await remote(Exec.command, "visudo", "-cf", "/etc/sudoers")
        result = await remote(
            Exec.command, "su", "-", ADMIN, "-c", "sudo -n id -u",
            capture_output=True,
        )
        assert result.stdout.strip() == b"0"


asyncio.run(configure_sudo())
```

Run the script again. `User.sudoer` writes `/etc/sudoers.d/deploy` with mode
`0440`. It grants this account passwordless access to **all commands as root**.
Use this rule for an account that you intend to make a full administrator.

`User.sudoer` does not validate the rule itself. The example runs `visudo`
after writing it, then executes `sudo -n id -u` as `deploy`. The assertion
checks that sudo actually runs as root without a password prompt.

The remaining steps keep the initial root connection. rmote does not use sudo
automatically when you connect as a user with a sudoers entry.

## Install Redis

Append this step to the same script:

<!-- name: test_quickstart; case: install -->
```python
async def install_redis():
    async with connect() as remote:
        await remote(Apt.update, ttl=3600)
        package = await remote(Apt.package, "redis-server")
        assert package.name == "redis-server" and package.version
        unchanged = await remote(Apt.package, "redis-server")
        assert not unchanged.changed
        print(f"Redis {package.version}: changed={package.changed}")


asyncio.run(install_redis())
```

Run `python deploy.py` again. `Apt.update` refreshes the package index, at most
once per hour here. `Apt.package` installs Redis if it is missing. Later runs
return `changed=False` for an already installed package.

These operations run as root in the container. On an SSH host, the login must
have the required permissions; rmote does not elevate privileges automatically.
The package also supplies the `redis` user and `redis-cli` command.
It may start the distribution's default service on port 6379. Our separate
cache uses port 6380 and does not replace that service.

## Render a systemd Service

The cache will listen on `127.0.0.1:6380`, with a 64 MiB memory limit.
It keeps no persistent data, so restarting it clears the cache.
Append this template to `deploy.py`:

<!-- name: test_quickstart; case: template -->
```python
from rmote.templates import Template

PORT = 6380
SERVICE = "rmote-cache.service"
UNIT_PATH = f"/etc/systemd/system/{SERVICE}"

unit = Template('''\
[Unit]
Description=Redis cache managed by rmote
After=network.target

[Service]
Type=notify
User=redis
Group=redis
ExecStart=/usr/bin/redis-server --bind 127.0.0.1 --port {{ port }} --supervised systemd --daemonize no --save "" --appendonly no --maxmemory {{ memory }} --maxmemory-policy allkeys-lru
Restart=on-failure
TimeoutStartSec=30

[Install]
WantedBy=multi-user.target
''').render(port=PORT, memory="64mb")
```

`{{ port }}` and `{{ memory }}` are template variables. `Template.render` replaces
them locally and returns a string. No template engine is needed on the target.
The {doc}`templating` guide also covers conditions, loops, and remote rendering.

`Type=notify` makes systemd wait for Redis to signal readiness before the start
operation completes. Redis uses this combination in its
[example systemd unit](https://github.com/redis/redis/blob/unstable/utils/systemd-redis_server.service).

## Write the Unit and Start the Service

Append the deployment step:

<!-- name: test_quickstart; case: service -->
```python
async def configure_cache():
    async with connect() as remote:
        try:
            previous = await remote(FileSystem.read_str, UNIT_PATH)
        except FileNotFoundError:
            previous = None

        if previous != unit:
            await remote(Exec.command, "tee", UNIT_PATH, stdin=unit)
            await remote(Service.daemon_reload)
            await remote(Service.restart, SERVICE)

        assert await remote(FileSystem.read_str, UNIT_PATH) == unit
        service = await remote(Service.converge, SERVICE, started=True, enabled=True)
        assert service.started and service.enabled
        unchanged = await remote(Service.converge, SERVICE, started=True, enabled=True)
        assert not unchanged.changed
        print(f"{service.name}: started={service.started}, enabled={service.enabled}")

        result = await remote(Exec.command, "redis-cli", "-p", str(PORT), "PING", capture_output=True)
        assert result.stdout.strip() == b"PONG"
        print(result.stdout.decode(), end="")


asyncio.run(configure_cache())
```

Run `python deploy.py`. The final output should include:

```text
rmote-cache.service: started=True, enabled=True
PONG
```

The script compares the remote file with the rendered template. If it changed,
`tee` writes the new content from standard input, systemd reloads its unit files,
and the service restarts. `Service.converge` ensures the service is running and
enabled at boot. An unchanged rerun does not rewrite the unit or restart Redis.

You can verify it outside Python too:

```bash
docker exec rmote-quickstart systemctl status rmote-cache.service --no-pager
docker exec rmote-quickstart redis-cli -p 6380 PING
```

The cache is available to applications on the target through its loopback
interface. No Redis port is exposed to your network.

## Check an Unchanged Rerun

Store a value, run the configuration step again, and check that the value
survives. This cache has no persistence, so a restart would lose the value.
Append this check to the script:

<!-- name: test_quickstart; case: unchanged_rerun -->
```python
async def check_rerun():
    async with connect() as remote:
        result = await remote(Exec.command, "redis-cli", "-p", str(PORT), "SET", "rmote:check", "hello", capture_output=True)
        assert result.stdout.strip() == b"OK"

    await configure_cache()

    async with connect() as remote:
        result = await remote(Exec.command, "redis-cli", "-p", str(PORT), "GET", "rmote:check", capture_output=True)
        assert result.stdout.strip() == b"hello"
        await remote(Exec.command, "redis-cli", "-p", str(PORT), "DEL", "rmote:check")


asyncio.run(check_rerun())
```

## Write a Tool for Your Application

Built-in tools covered installation and service management. Add your own tool
when you want to group application-specific work and return Python values.
For example, read Redis statistics without parsing them in every client.

Save this as `cache_tools.py` beside `deploy.py`:

```{literalinclude} ../examples/quickstart/cache_tools.py
:language: python
```

Append this call to `deploy.py`:

<!-- name: test_quickstart; case: custom -->
```python
from cache_tools import Cache


async def show_cache_stats():
    async with connect() as remote:
        stats = await remote(Cache.stats, PORT)
        assert int(stats["total_commands_processed"]) > 0
        print("Commands processed:", stats["total_commands_processed"])


asyncio.run(show_cache_stats())
```

Run `python deploy.py` again. rmote transfers `cache_tools.py` on the first call,
executes the method in the container, and returns a Python dictionary.
You do not need to install or copy your tool separately on the target.

Keep the tool in its own module because rmote transfers the whole module.
The `process` helper captures command output without interfering with the
connection. Continue with {doc}`writing-tools` for return types and dependencies.

## Use the Same Deployment over SSH

On a Debian or Ubuntu host with systemd and Python 3.11 or newer, first check
that `ssh root@server python3 --version` works. Then replace only `connect()`
in `deploy.py`:

<!-- name: test_quickstart; case: ssh -->
```python
@asynccontextmanager
async def connect():
    async with await Protocol.from_ssh("root@server") as remote:
        yield remote
```

Run the same script. Package installation, template rendering, service
management, and your custom tool need no changes. Replace `root@server` with
your target; this run installs software and creates a service there.

For a specific key or port, pass `identity="/path/to/key"` or `port=2222` to
`Protocol.from_ssh`.

## Clean Up and Continue

When you finish with the Docker example, remove its disposable host:

```bash
docker stop rmote-quickstart
```

The container was started with `--rm`, so stopping it removes its files too.
Delete the local `quickstart_key` and `quickstart_key.pub` files if you created
them only for this tutorial.

- {doc}`writing-tools`: build reusable tools and return structured results.
- {doc}`templating`: render more complex configuration files.
- {doc}`multi-host`: deploy to several hosts.
- {doc}`api/sync`: use the synchronous client in applications without asyncio.
- {doc}`concepts`: understand execution, resource ownership, and cancellation.
