from app.agent.citations import build_structured_answer
from app.agent.context import CaseContext
from app.agent.retrieval import Hit


def hit(n=1, para=3):
    return Hit(n, "doc", "Intervention", "Rogers", "2026-01-02", para, para, 2, "https://x.crtc.gc.ca/a", "text " * 200)


def test_valid_answer_with_headings_and_note():
    reg = {"c1": hit(1), "c2": hit(2, 9)}
    text = (
        "Main themes\n\nParties want lower rates [c1][c2].\n\nRogers supports it [c2].\n\n"
        "Coverage note: 5 of 5 documents searchable."
    )
    out = build_structured_answer(text, reg)
    assert out["citation_valid"] and out["warnings"] == []
    kinds = [p["kind"] for p in out["paragraphs"]]
    assert kinds == ["heading", "body", "body", "note"]
    assert [c["id"] for c in out["paragraphs"][1]["citations"]] == ["c1", "c2"]
    assert out["paragraphs"][1]["citations"][0]["snippet"].endswith("…")
    assert out["n_citations"] == 2


def test_uncited_paragraph_and_unknown_id_are_flagged():
    out = build_structured_answer("This claims things without support.\n\nOther [c9].", {"c1": hit()})
    assert not out["citation_valid"]
    assert any("without a citation" in w for w in out["warnings"])
    assert any("[c9]" in w for w in out["warnings"])
    assert out["paragraphs"][1]["citations"] == []


def test_no_registry_flags_substantive_answer_but_not_when_citations_not_required():
    assert not build_structured_answer("Plenty of claims here.", {})["citation_valid"]
    assert build_structured_answer("Plenty of claims here.", {}, require_citations=False)["citation_valid"]


def test_registry_dedups_same_chunk():
    import uuid

    ctx = CaseContext(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "2026-1")
    assert ctx.register(hit(1)) == "c1"
    assert ctx.register(hit(1)) == "c1"
    assert ctx.register(hit(2)) == "c2"
