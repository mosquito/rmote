"""Synchronize files or directory contents through an arbitrary transport."""

import argparse
import asyncio
import logging
import shlex
import sys
import time
from pathlib import Path

from rmote.protocol import Protocol
from rmote.tools.file_sync import FileSync
from rmote.tools.rsync import Result, Rsync

log = logging.getLogger(__name__)


def transport_command(value: str) -> list[str]:
    """Parse shell-style quoting without executing a shell."""
    try:
        command = shlex.split(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if not command:
        raise argparse.ArgumentTypeError("The transport command must not be empty")
    return command


async def transfer(args: argparse.Namespace) -> Result:
    """Dispatch directory trees to Rsync and individual files to FileSync."""
    uploading = args.target.startswith("remote:")
    source = args.source.removeprefix("remote:")
    target = args.target.removeprefix("remote:")
    python = args.python or ("python3" if args.transport else sys.executable)
    async with await Protocol.from_command(*args.transport, python=python) as remote:
        entry = (
            await remote(Rsync.inspect, source) if not uploading else await asyncio.to_thread(Rsync.inspect, source)
        )
        if entry is None:
            raise FileNotFoundError(f"Source does not exist: {args.source}")
        if entry.kind == "directory":
            method = Rsync.upload if uploading else Rsync.download
            return await method(
                remote,
                source,
                target,
                delete=args.delete,
                delete_excluded=args.delete_excluded,
                concurrency=args.concurrency,
                preserve_mode=args.preserve_mode,
                block_size=args.block_size,
                owner=args.owner,
                group=args.group,
                exclude=args.exclude,
            )
        if entry.kind != "file":
            raise ValueError("The source must be a regular file or a directory")
        if args.delete or args.delete_excluded or args.exclude:
            raise ValueError("--delete, --delete-excluded and --exclude apply to directory sources only")
        destination = (
            await remote(Rsync.inspect, target) if uploading else await asyncio.to_thread(Rsync.inspect, target)
        )
        if destination is not None and destination.kind == "directory":
            target = str(Path(target) / Path(source).name)
        # Validate requested ownership before replacing any contents.
        ownership = None
        if args.owner is not None or args.group is not None:
            ownership = (
                await remote(Rsync.ownership, args.owner, args.group)
                if uploading
                else await asyncio.to_thread(Rsync.ownership, args.owner, args.group)
            )
        copy = FileSync.upload if uploading else FileSync.download
        item = await copy(remote, source, target, block_size=args.block_size)
        result = Result(changed=item.changed, files=1, transferred=item.transferred, reused=item.reused)
        if ownership is not None:
            result.changed |= (
                await remote(Rsync.chown, target, *ownership)
                if uploading
                else await asyncio.to_thread(Rsync.chown, target, *ownership)
            )
        if args.preserve_mode:
            result.changed |= (
                await remote(Rsync.chmod, target, entry.mode)
                if uploading
                else await asyncio.to_thread(Rsync.chmod, target, entry.mode)
            )
        if item.changed:
            log.info("file %r: %d bytes transferred, %d reused", source, item.transferred, item.reused)
        return result


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Register transfer options and examples on the shared CLI."""
    parser.description = (
        "Synchronize a file or directory contents. Mark exactly one endpoint with remote:.\n\n"
        "The remote side needs only Python 3.11+ and its standard library.\n"
        "No rsync, rmote installation or third-party packages are required there.\n"
        "rmote sends the required code over the connection.\n"
        "The transport command receives Python and -qui automatically."
    )
    parser.epilog = """Examples:
  rmote rsync -r 'ssh -T server' ./project remote:/srv/project
  rmote rsync --exec 'ssh -T server' remote:/srv/project ./copy
  rmote rsync -r 'docker exec -i my-container' ./project remote:/app
  rmote rsync -r 'ssh -T server' ./config.ini remote:/etc/app/config.ini
  rmote rsync -x '.git/' --delete -r 'ssh -T server' ./project remote:/srv/project
  rmote rsync -x '*.pyc' --delete-excluded -r 'docker exec -i my-container' . remote:/app

Use Docker exec -i without -t; SSH -T disables its terminal.
-r selects the transport command; directories are always recursive.
Directory contents go directly into the destination (a trailing slash has no special meaning).
Extra destination entries are kept unless --delete is supplied.
--delete-excluded also deletes excluded destination entries and implies --delete.
Logs and transfer totals go to stderr. Omit --exec for a local Python subprocess.
"""
    parser.set_defaults(__func__=run, __prog__=parser.prog, __parser__=parser)
    parser.add_argument(
        "-r",
        "--exec",
        dest="transport",
        type=transport_command,
        default=[],
        metavar="COMMAND",
        help="Transport command with shell-style quoting, e.g. 'ssh -T server'.",
    )
    parser.add_argument("-p", "--python", help="Remote Python executable.")
    parser.add_argument(
        "-d", "--delete", action="store_true", help="Delete extra destination entries after successful transfers."
    )
    parser.add_argument(
        "-D",
        "--delete-excluded",
        action="store_true",
        help="Also delete excluded destination entries; implies --delete.",
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="extend",
        nargs="*",
        default=[],
        metavar="PATTERN",
        help="Exclude path globs; repeatable. Put -- before SOURCE and DEST after the patterns.",
    )
    parser.add_argument(
        "-j",
        "--concurrency",
        type=int,
        default=32,
        metavar="N",
        help="Simultaneous file transfers.",
    )
    parser.add_argument(
        "-B",
        "--block-size",
        type=int,
        default=4 * 1024 * 1024,
        metavar="BYTES",
        help="Block size in bytes (maximum: 16777216).",
    )
    parser.add_argument(
        "-P", "--no-preserve-mode", dest="preserve_mode", action="store_false", help="Keep destination permissions."
    )
    parser.add_argument("-o", "--owner", help="Set destination ownership to this username.")
    parser.add_argument("-g", "--group", help="Set destination group to this name.")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-q", "--quiet", action="store_true", help="Only report errors.")
    verbosity.add_argument(
        "-v", "--debug", action="store_true", help="Include protocol debug logging and error tracebacks."
    )
    parser.add_argument("source", metavar="SOURCE", help="Source file or directory; use remote: for downloads.")
    parser.add_argument("target", metavar="DEST", help="Destination path; use remote: for uploads.")


def run(args: argparse.Namespace) -> int:
    """Validate endpoints, run the transfer and report content statistics."""
    if args.source.startswith("remote:") == args.target.startswith("remote:"):
        args.__parser__.error("Exactly one of SOURCE and DEST must start with remote:")
    if not args.source.removeprefix("remote:") or not args.target.removeprefix("remote:"):
        args.__parser__.error("Source and destination paths must not be empty")
    if args.concurrency < 1:
        args.__parser__.error("--concurrency must be positive")
    if not 1 <= args.block_size <= 16 * 1024 * 1024:
        args.__parser__.error("--block-size must be between 1 and 16777216")
    level = logging.DEBUG if args.debug else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING, format="%(message)s", stream=sys.stderr
    )
    logging.getLogger("rmote").setLevel(level)
    started = time.monotonic()
    log.debug("%r -> %r", args.source, args.target)
    try:
        result = asyncio.run(transfer(args))
    except KeyboardInterrupt:
        print(f"{args.__prog__}: interrupted", file=sys.stderr)
        return 130
    except Exception as error:
        if args.debug:
            log.exception("Transfer failed")
        else:
            print(f"{args.__prog__}: {error}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started
    log.info(
        "%s: %d files checked, %d bytes transferred, %d bytes reused; "
        "%d directories created, %d symlinks updated, %d entries deleted; %.2fs (%.2f MiB/s)",
        "Updated" if result.changed else "Up to date",
        result.files,
        result.transferred,
        result.reused,
        result.directories,
        result.symlinks,
        result.deleted,
        elapsed,
        result.transferred / max(elapsed, 1e-9) / (1024 * 1024),
    )
    return 0
