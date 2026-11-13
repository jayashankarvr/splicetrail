"""`python -m splicetrail` messages for commands that can't run."""

import sys

from splicetrail import __main__ as entry


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["python -m splicetrail", *argv])
    return entry.main()


def test_test_command_outside_a_checkout_explains_and_fails(
    monkeypatch, tmp_path, capsys
):
    # An installed package has no tests directory three levels up.
    fake = tmp_path / "site-packages" / "splicetrail" / "__main__.py"
    monkeypatch.setattr(entry, "__file__", str(fake))
    assert _run(monkeypatch, "test") == 1
    err = capsys.readouterr().err
    assert "source checkout" in err
    assert 'pip install -e ".[dev]"' in err


def test_test_command_without_pytest_explains_and_fails(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pytest", None)  # import pytest -> ImportError
    assert _run(monkeypatch, "test") == 1
    err = capsys.readouterr().err
    assert "pytest is not installed" in err
    assert 'pip install -e ".[dev]"' in err


def test_match_import_error_does_not_suggest_a_missing_extra(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "splicetrail.cli", None)
    assert _run(monkeypatch, "match", "a.png", "b.png") == 1
    out = capsys.readouterr()
    assert "splicetrail[cli]" not in out.out + out.err
    assert "could not load the splicetrail CLI" in out.err
