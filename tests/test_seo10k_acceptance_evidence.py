from scripts.accept_seo10k_foundation import (
    IMPLEMENTATION_GATE_SPECS,
    RELEASE_GATES,
    build_evidence,
)


def test_evidence_keeps_implementation_and_canonical_release_gates_separate():
    gate_results = {
        gate_id: {
            "status": "PASS",
            "command": ["python", "-m", "pytest", *test_ids, "-q", "-rA"],
            "returncode": 0,
            "summary": f"{len(test_ids)} passed",
        }
        for gate_id, _description, test_ids in IMPLEMENTATION_GATE_SPECS
    }
    evidence = build_evidence(
        head_sha="a" * 40,
        source_tree_sha="b" * 40,
        gate_results=gate_results,
        generated_at="2026-09-27T00:00:00+00:00",
    )
    assert evidence["task_id"] == "SEO-10K-IMPL-01-A-CORRECTION-02"
    assert evidence["head_sha"] == "a" * 40
    assert evidence["source_tree_sha"] == "b" * 40
    assert evidence["canonical_contract_sha256"] == (
        "051b13f14a09d25f6bc9f7ec1079cab65bbc02db0c439e33838b32010bfdd599"
    )
    assert list(evidence["implementation_gates"]) == [
        f"SEOIMPL-A{number:02d}" for number in range(1, 19)
    ]
    assert list(evidence["release_gates"]) == list(RELEASE_GATES)
    assert all(
        item["status"] == "PASS"
        for item in evidence["implementation_gates"].values()
    )
    assert all(
        item["status"] == "NOT_RUN" and item["release_blocking"] is True
        for item in evidence["release_gates"].values()
    )
    assert evidence["release_blocked"] is True
    assert set(evidence["waves"].values()) == {"NOT_RELEASED"}
    assert all(
        item["test_ids"] and item["evidence_command"]
        for item in evidence["implementation_gates"].values()
    )
    assert not (set(evidence["implementation_gates"]) & set(evidence["release_gates"]))


def test_canonical_gate_meanings_are_exact_and_immutable():
    assert RELEASE_GATES == {
        "SEO10K-A01": "Input integrity",
        "SEO10K-A02": "Entity scope",
        "SEO10K-A03": "Public boundary",
        "SEO10K-A04": "Numeric Index off",
        "SEO10K-A05": "Semantic compiler",
        "SEO10K-A06": "Demand evidence",
        "SEO10K-A07": "Cohort manifest",
        "SEO10K-A08": "Index eligibility",
        "SEO10K-A09": "SSR consistency",
        "SEO10K-A10": "URL contract",
        "SEO10K-A11": "Metadata",
        "SEO10K-A12": "Structured data",
        "SEO10K-A13": "Thin content",
        "SEO10K-A14": "Freshness/dispute",
        "SEO10K-A15": "robots/meta",
        "SEO10K-A16": "Sitemap",
        "SEO10K-A17": "Catalog/navigation",
        "SEO10K-A18": "Cache/republish",
        "SEO10K-A19": "Reliability",
        "SEO10K-A20": "Recovery",
        "SEO10K-A21": "Search consoles",
        "SEO10K-A22": "Legal/error path",
    }
