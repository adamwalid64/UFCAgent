from __future__ import annotations

from pathlib import Path

from pipeline.run import _targets_canonical_database


def test_direct_run_identifies_relative_and_absolute_canonical_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    canonical = tmp_path / "ufc_fights.db"

    assert _targets_canonical_database("sqlite:///./ufc_fights.db")
    assert _targets_canonical_database(f"sqlite:///{canonical.as_posix()}")


def test_direct_run_allows_noncanonical_targets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    assert not _targets_canonical_database("sqlite:///./smoke.db")
    assert not _targets_canonical_database("sqlite:///:memory:")
    assert not _targets_canonical_database("postgresql://localhost/ufc")
