"""The doctor must read the scheduled command the way the shell does.

Regression: an install under a folder with a space in its name (an external
volume) gets a single-quoted absolute path in the plist. The old regex saw
`hd' run-once`, reported "could not find an hd invocation", and would have
mangled the path at the space had it matched.
"""

from __future__ import annotations

from pathlib import Path

from hd.doctor import OK, WARN, _check_interpreter, _hd_invocations


def _command(project: Path) -> str:
    hd = f"'{project}/.venv/bin/hd'"
    return (
        f"export PYTHONUNBUFFERED=1; cd '{project}' && {hd} run-once && {hd} notify"
    )


def test_finds_quoted_path_with_spaces(tmp_path: Path) -> None:
    project = tmp_path / "CRUCIAL 525GB" / "suwanee-clearance-watch"
    (project / ".venv" / "bin").mkdir(parents=True)
    hd = project / ".venv" / "bin" / "hd"
    hd.write_text("#!/bin/sh\n")

    assert set(_hd_invocations(_command(project))) == {str(hd)}

    checks = list(
        _check_interpreter({"ProgramArguments": ["/bin/bash", "-lc", _command(project)]},
                           Path("com.test.plist"))
    )
    assert [c.status for c in checks] == [OK]


def test_unquoted_path_still_works(tmp_path: Path) -> None:
    hd = tmp_path / ".venv" / "bin" / "hd"
    hd.parent.mkdir(parents=True)
    hd.write_text("#!/bin/sh\n")
    assert _hd_invocations(f"{hd} run-once") == [str(hd)]


def test_no_hd_in_command_still_warns() -> None:
    checks = list(
        _check_interpreter({"ProgramArguments": ["/bin/bash", "-lc", "echo hello"]},
                           Path("com.test.plist"))
    )
    assert [c.status for c in checks] == [WARN]
