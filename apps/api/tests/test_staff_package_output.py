"""How the edge recognition package leaves the process (V1-02B0).

From this stage the package can carry real adult biometric templates, so the destination is
part of the security boundary rather than a convenience. These tests need no database: they
exercise the writer directly with a package-shaped dictionary.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from veotrex_api.staff_package import (
    PackageOutputRefused,
    parser,
    write_package,
)

# Shaped like a real package, with template material in the place real templates occupy.
TEMPLATE_MATERIAL = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5"
PACKAGE = {
    "schema_version": 1,
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "model_id": "yunet+sface",
    "model_version": "2023mar+2021dec",
    "template_version": 1,
    "revision": "f" * 64,
    "generated_at": "2026-09-22T00:00:00+00:00",
    "staff": [
        {
            "staff_id": "22222222-2222-2222-2222-222222222222",
            "display_name": "Chandra",
            "templates": [
                {
                    "template_id": "33333333-3333-3333-3333-333333333333",
                    "dimensions": 128,
                    "dtype": "float32",
                    "quality": 90,
                    "data_base64": TEMPLATE_MATERIAL,
                }
            ],
        }
    ],
}


def test_a_written_package_is_private_to_its_owner(tmp_path: Path) -> None:
    output = tmp_path / "package.json"
    write_package(PACKAGE, output)
    mode = stat.S_IMODE(output.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert json.loads(output.read_text())["staff"][0]["display_name"] == "Chandra"


def test_writing_over_an_existing_file_is_refused(tmp_path: Path) -> None:
    """The file is created exclusively, so there is no pre-existing mode or owner to inherit -
    and no chance of quietly replacing something else with biometric material."""
    output = tmp_path / "package.json"
    output.write_text("someone else's file")
    with pytest.raises(PackageOutputRefused) as raised:
        write_package(PACKAGE, output)
    assert "already exists" in str(raised.value)
    assert output.read_text() == "someone else's file"


def test_writing_to_a_world_readable_file_that_already_exists_is_refused(
    tmp_path: Path,
) -> None:
    """The failure this prevents: an existing 0644 file keeps its mode, because the creation
    mode only applies when the file is created. Refusing to reuse it closes that entirely."""
    output = tmp_path / "package.json"
    output.touch(mode=0o644)
    with pytest.raises(PackageOutputRefused):
        write_package(PACKAGE, output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert output.read_text() == ""


def test_writing_through_a_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "somewhere-readable.json"
    target.touch(mode=0o644)
    link = tmp_path / "package.json"
    os.symlink(target, link)
    with pytest.raises(PackageOutputRefused):
        write_package(PACKAGE, link)
    assert target.read_text() == ""


def test_writing_to_stdout_is_refused(tmp_path: Path) -> None:
    """``--output /dev/stdout`` is the accident this exists for: it would print every template
    to a terminal, a shell scrollback and any pipeline capturing the output."""
    with pytest.raises(PackageOutputRefused):
        write_package(PACKAGE, Path("/dev/stdout"))


def test_writing_into_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PackageOutputRefused) as raised:
        write_package(PACKAGE, tmp_path / "absent" / "package.json")
    assert "cannot be created" in str(raised.value)


def test_a_failed_write_leaves_no_partial_file_behind(tmp_path: Path) -> None:
    """A half-written package still contains real templates, so it must not survive."""
    output = tmp_path / "package.json"

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        write_package({**PACKAGE, "broken": Unserialisable()}, output)
    assert not output.exists()


def test_the_cli_documents_that_it_will_not_write_to_a_terminal() -> None:
    help_text = parser().format_help()
    assert "/dev/stdout" in help_text
    assert "0600" in help_text


def test_the_cli_has_no_statement_that_could_print_template_material() -> None:
    """The tool reports a count and a truncated revision and nothing else. Asserted against the
    source, because the failure mode is a debugging ``print`` added later that reaches a
    terminal, a shell history or a CI log with real biometrics in it."""
    from veotrex_api import staff_package

    source = Path(staff_package.__file__).read_text(encoding="utf-8")
    printed = [line.strip() for line in source.splitlines() if line.strip().startswith("print(")]
    assert printed, "the tool is expected to report what it wrote"
    for statement in printed:
        assert "template" not in statement.lower()
        assert "data_base64" not in statement
        assert "entries" not in statement
