from pathlib import Path

import pytest

from devtools.verification import offline, review_context


@pytest.mark.unit
def test_harness_evidence_retains_counts_without_raw_output():
    evidence = review_context.harness_test_evidence(
        "secret=https://example.invalid/key\n==> tests: /private/python -m pytest\n"
        "1591 passed, 12 deselected in 90.31s\n==> lint: ruff\nAll checks passed!"
    )
    assert [item.command for item in evidence] == [
        "python -m pytest tests/ -q -m 'not integration'",
        "python -m ruff check .",
        "git diff --check",
    ]
    assert all(item.status == "pass" for item in evidence)
    assert "1591 passed, 12 deselected in 90.31s" in evidence[0].summary
    assert all("harness" in item.summary for item in evidence)
    assert "secret" not in repr(evidence)
    assert "/private" not in repr(evidence)


@pytest.mark.unit
def test_harness_evidence_tracks_authoritative_gate_changes(monkeypatch):
    original_build_steps = offline.build_steps

    def revised_steps(repo_root, python_executable, only="all"):
        tests, lint, diff = original_build_steps(repo_root, python_executable, only)
        return (
            diff,
            offline.GateStep(tests.name, (*tests.argv, "--strict-markers")),
            lint,
        )

    monkeypatch.setattr(offline, "build_steps", revised_steps)
    summary = "7 passed in 0.01s"
    evidence = review_context.harness_test_evidence(summary)

    assert [item.command for item in evidence] == [
        "git diff --check",
        "python -m pytest tests/ -q -m 'not integration' --strict-markers",
        "python -m ruff check .",
    ]
    assert summary in evidence[1].summary
    assert all(summary not in evidence[index].summary for index in (0, 2))


@pytest.mark.unit
def test_harness_evidence_accepts_real_pytest_subtest_and_duration_summary():
    summary = "1612 passed, 1 skipped, 1 deselected, 18 warnings, 85 subtests passed in 93.48s (0:01:33)"
    assert summary in review_context.harness_test_evidence(summary)[0].summary


@pytest.mark.unit
def test_gate_definition_accepts_exact_imported_source(tmp_path):
    target = tmp_path / "devtools/verification/offline.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(Path(offline.__file__).read_bytes())

    assert review_context.validate_gate_definition(tmp_path) is None


@pytest.mark.unit
def test_gate_definition_rejects_different_target_steps_without_source_disclosure(tmp_path):
    target = tmp_path / "devtools/verification/offline.py"
    target.parent.mkdir(parents=True)
    target.write_text("def build_steps(*args):\n    return ()  # private target detail\n")

    with pytest.raises(ValueError, match="gate definition") as error:
        review_context.validate_gate_definition(tmp_path)
    assert str(tmp_path) not in str(error.value)
    assert "private target detail" not in str(error.value)


@pytest.mark.unit
def test_gate_definition_rejects_symlink_even_to_matching_source(tmp_path):
    target = tmp_path / "devtools/verification/offline.py"
    target.parent.mkdir(parents=True)
    target.symlink_to(Path(offline.__file__))

    with pytest.raises(ValueError, match="gate definition"):
        review_context.validate_gate_definition(tmp_path)


@pytest.mark.unit
def test_gate_definition_rejects_missing_target(tmp_path):
    with pytest.raises(ValueError, match="gate definition"):
        review_context.validate_gate_definition(tmp_path)


@pytest.mark.unit
def test_gate_definition_rejects_unreadable_target_without_path_disclosure(tmp_path, monkeypatch):
    target = tmp_path / "devtools/verification/offline.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(Path(offline.__file__).read_bytes())
    original_read_bytes = Path.read_bytes

    def unreadable(path):
        if path == target:
            raise PermissionError("private filesystem detail")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(ValueError, match="gate definition") as error:
        review_context.validate_gate_definition(tmp_path)
    assert str(tmp_path) not in str(error.value)
    assert "private filesystem detail" not in str(error.value)


@pytest.mark.unit
def test_navigation_references_real_lines_and_declares_omissions(tmp_path):
    (tmp_path / "sample.py").write_text("import os\n\ndef useful():\n    pass\n")
    (tmp_path / "guide.md").write_text("# Contract\n\n## Safety\n")
    index = review_context.navigation_index(tmp_path, ["sample.py", "gone.py"], explicit=["guide.md"])
    assert "sample.py:3" in index and "useful" in index
    assert "guide.md:3" in index and "Safety" in index
    assert "gone.py" in index and "unavailable" in index
    assert str(tmp_path) not in index


@pytest.mark.unit
@pytest.mark.parametrize("path", ["../secret.py", "/secret.py", ".env", ".git/config", "missing.md"])
def test_navigation_rejects_unsafe_or_missing_explicit_context(tmp_path, path):
    with pytest.raises(ValueError, match="context"):
        review_context.navigation_index(tmp_path, [], explicit=[path])


@pytest.mark.unit
def test_navigation_refuses_symlinks_even_to_other_checkout_files(tmp_path):
    outside = tmp_path.parent / "outside-review-context.md"
    outside.write_text("# private data\n")
    (tmp_path / "linked.md").symlink_to(outside)
    with pytest.raises(ValueError, match="context"):
        review_context.navigation_index(tmp_path, [], explicit=["linked.md"])


@pytest.mark.unit
def test_navigation_does_not_follow_directory_symlink(tmp_path):
    outside = tmp_path.parent / "outside-review-directory"
    outside.mkdir()
    (outside / "safe.md").write_text("# outside\n")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="context"):
        review_context.navigation_index(tmp_path, [], explicit=["linked/safe.md"])


@pytest.mark.unit
def test_navigation_bounds_index_without_silently_claiming_full_coverage(tmp_path):
    (tmp_path / "large.md").write_text("\n".join(f"## Section {n}" for n in range(100)))
    index = review_context.navigation_index(tmp_path, ["large.md"])
    assert "omitted" in index
    assert len(index.splitlines()) < 70
    assert Path("large.md").name in index
