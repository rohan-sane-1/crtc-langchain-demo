"""Turn the agent's final text into a structured answer with validated citations.

Pure function (no LLM, no DB). Rules:
  * paragraphs are separated by blank lines;
  * every body paragraph must contain at least one ``[cN]`` marker;
  * every marker must exist in this run's registry (the passages the tools actually returned);
  * headings (short single lines without a period) and the closing "Coverage note" line are exempt.
"""

import re
from typing import Any

from app.agent.retrieval import Hit

MARKER = re.compile(r"\[c(\d+)\]")
SNIPPET_CHARS = 400


def _is_heading(p: str) -> bool:
    line = p.strip().lstrip("#*_ ").rstrip("*_: ")
    return "\n" not in p.strip() and len(line) <= 80 and not line.endswith((".", "!", "?"))


def _is_note(p: str) -> bool:
    return p.strip().lower().lstrip("*_# ").startswith("coverage note")


def _citation(cid: str, h: Hit) -> dict[str, Any]:
    text = " ".join(h.text.split())
    return {
        "id": cid,
        "doc_title": h.doc_title,
        "filer": h.filer,
        "filed_date": h.filed_date,
        "para_start": h.para_start,
        "para_end": h.para_end,
        "page": h.page,
        "url": h.url,
        "snippet": text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else ""),
    }


def build_structured_answer(
    text: str, registry: dict[str, Hit], *, require_citations: bool = True
) -> dict[str, Any]:
    paragraphs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw in re.split(r"\n\s*\n", text.strip()):
        p = raw.strip()
        if not p:
            continue
        ids = list(dict.fromkeys(f"c{n}" for n in MARKER.findall(p)))
        known = [i for i in ids if i in registry]
        unknown = [i for i in ids if i not in registry]
        if unknown:
            warnings.append(f"Unknown citation(s) {', '.join(f'[{i}]' for i in unknown)}")
        if ids:
            kind = "body"
        elif _is_note(p):
            kind = "note"
        elif _is_heading(p):
            kind = "heading"
        else:
            kind = "body"
            if require_citations:
                warnings.append(f"Paragraph without a citation: “{p[:60]}…”")
        paragraphs.append(
            {"text": p, "kind": kind, "citations": [_citation(i, registry[i]) for i in known]}
        )
    if require_citations and not registry and any(p["kind"] == "body" for p in paragraphs):
        warnings.append("No documents were searched for this reply.")
    return {
        "paragraphs": paragraphs,
        "citation_valid": not warnings,
        "warnings": warnings,
        "n_citations": len({c["id"] for p in paragraphs for c in p["citations"]}),
    }


def repair_prompt(warnings: list[str]) -> str:
    return (
        "Your last answer failed citation validation:\n- "
        + "\n- ".join(warnings[:6])
        + "\nRewrite the complete answer. Every body paragraph must end with one or more [cN] "
        "markers taken from passages returned by the search tools in THIS turn (search again if "
        "needed). Remove claims you cannot support."
    )
