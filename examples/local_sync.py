"""Run with python examples/local_sync.py after installing rmote."""

from lifecycle_tools import Echo

from rmote.sync import Connection


def main() -> None:
    with Connection.from_local(rpc_timeout=5.0) as remote:
        assert remote(Echo.echo, "hello") == "hello"
        assert remote(Echo.later, "again") == "again"
        assert remote.call_with_timeout(2.0, Echo.echo, "deadline") == "deadline"
        try:
            remote.call_with_timeout(0.05, Echo.later, "slow", delay=0.5)
        except TimeoutError:
            print("Local waiting stopped; the remote operation can continue.")
        else:
            raise AssertionError("The delayed call must exceed its deadline")
        assert remote(Echo.echo, "still open") == "still open"


if __name__ == "__main__":
    main()
