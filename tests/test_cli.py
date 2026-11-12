import pytest
from click.testing import CliRunner

from engine.cli import STUB_COMMANDS, main


@pytest.mark.parametrize("name", STUB_COMMANDS)
def test_stub_commands_exit_1(name: str) -> None:
    result = CliRunner().invoke(main, [name])
    assert result.exit_code == 1
    assert result.output.strip() == f"{name}: not implemented"
