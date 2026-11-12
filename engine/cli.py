"""Command-line entry point: `te`."""

import sys

import click

STUB_COMMANDS = (
    "find",
    "understand",
    "judge",
    "build",
    "draft",
    "send",
    "smoke",
)


@click.group()
def main() -> None:
    """Teardown engine."""


def _make_stub(name: str) -> click.Command:
    @click.command(name=name, help=f"{name} (not implemented).")
    def _stub() -> None:
        click.echo(f"{name}: not implemented")
        sys.exit(1)

    return _stub


for _name in STUB_COMMANDS:
    main.add_command(_make_stub(_name))


if __name__ == "__main__":
    main()
