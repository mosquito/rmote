"""Shared command line interface for ``rmote`` and ``python -m rmote``."""

import argparse
from collections.abc import Callable

from rmote.cli import shell


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Show argparse defaults while preserving multiline examples."""


def build_parser(prog: str = "rmote") -> argparse.ArgumentParser:
    """Build the common CLI with a handler registered for each subcommand."""
    parser = argparse.ArgumentParser(
        prog=prog, description="Run shells and tools over an rmote connection.", formatter_class=HelpFormatter
    )
    subparsers = parser.add_subparsers(required=True, title="commands")
    shell.configure_parser(subparsers.add_parser("shell", help="Open an interactive terminal shell."))
    for command in subparsers.choices.values():
        command.formatter_class = HelpFormatter
    return parser


def main(argv: list[str] | None = None, *, prog: str = "rmote") -> int:
    """Parse command arguments and call the selected handler."""
    args = build_parser(prog).parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.__func__
    return handler(args)
