from scripts.accept_seo10k_foundation import build_matrix


def test_evidence_matrix_is_complete_non_averaged_and_not_run_blocks_release():
    evidence = build_matrix(focused_passed=True, command=["python", "-m", "pytest"])
    assert list(evidence["gates"]) == [f"A{number:02d}" for number in range(1, 23)]
    assert evidence["no_averaged_score"] is True
    assert evidence["release_blocked"] is True
    assert evidence["gates"]["A01"]["status"] == "PASS"
    for gate in ("A06", "A07", "A19", "A20", "A21", "A22"):
        assert evidence["gates"][gate]["status"] == "NOT_RUN"
        assert evidence["gates"][gate]["release_blocking"] is True
    assert set(evidence["waves"].values()) == {"NOT_RELEASED"}
