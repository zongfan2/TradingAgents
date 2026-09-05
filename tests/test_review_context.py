from pathlib import Path

import pytest

from devtools.verification import review_context


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
def test_harness_evidence_accepts_real_pytest_subtest_and_duration_summary():
    summary = "1612 passed, 1 skipped, 1 deselected, 18 warnings, 85 subtests passed in 93.48s (0:01:33)"
    assert summary in review_context.harness_test_evidence(summary)[0].summary


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
