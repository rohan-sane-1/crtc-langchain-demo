"""Per-run context handed to every tool. The model never supplies case/user/chat ids."""

import uuid
from dataclasses import dataclass, field

from app.agent.retrieval import Hit


@dataclass
class CaseContext:
    user_id: uuid.UUID
    chat_id: uuid.UUID
    case_id: uuid.UUID
    notice_number: str
    # Citation registry for THIS run: "c3" -> Hit. Filled by the search tools, read by
    # citations.build_structured_answer. Ids are only valid within the turn that created them.
    registry: dict[str, Hit] = field(default_factory=dict)
    _keys: dict[tuple, str] = field(default_factory=dict)

    def register(self, hit: Hit) -> str:
        key = (hit.document_id, hit.para_start, hit.para_end, hit.chunk_id)
        if key not in self._keys:
            cid = f"c{len(self._keys) + 1}"
            self._keys[key] = cid
            self.registry[cid] = hit
        return self._keys[key]
