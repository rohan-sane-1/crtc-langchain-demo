# LangSmith: how to see how the Copilot performs and where it gets its information

Tracing is on when `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` are set in `backend/.env`
(`app/main.py` exports `.env` to the process environment at startup). Traces go to the agent
`LANGSMITH_AGENT_ID` (`langchain-rag-rca`) in environment `LANGSMITH_AGENT_ENVIRONMENT` (`local`),
via `LANGSMITH_ENDPOINT` (`https://api.smith.langchain.com`). Don't also set `LANGSMITH_PROJECT`.

## What one chat turn looks like

Every message (and the automatic "check the knowledge base" turn when a chat opens) is one root
trace named **`crtc_chat_turn`**:

```
crtc_chat_turn
└─ crtc_copilot (the Deep Agent)
   ├─ model call (Gemini)
   ├─ check_kb_status ─ check_case_missing_documents ─ records.list_record_documents
   ├─ refresh_knowledge_base ─ sync_case_knowledge_base      (only when something is missing)
   ├─ search_case_documents ─ retrieval.hybrid_search          (run type: retriever)
   └─ model call (final answer)
```

**Where the answer's information came from:** open the `retrieval.hybrid_search` span. Its output
lists the exact chunks returned (`page_content`, plus `doc_title`, paragraph range, filer and URL
in metadata). The root run's outputs also hold `sources`, the full citation registry for the turn.

## Metadata and tags on every root run

| Field | Meaning |
|---|---|
| `user_hash` | first 12 hex chars of SHA-256(user id). Pseudonymous; never the email |
| `chat_id` | chat = LangGraph thread id |
| `case_id`, `notice_number` | the CRTC proceeding this chat is about |
| `app_version`, `model` | code version and Gemini model id used |
| `turn_kind` | `open` (automatic coverage check) or `question` |
| tags | `chat`, `case:<notice number>` |

## Feedback attached to each root run

| Key | Source |
|---|---|
| `citation_valid` | 1 = every body paragraph cites passages retrieved in that turn; 0 = validation failed even after one repair attempt |
| `n_citations` | number of distinct sources cited |
| `user_score` | thumbs up (1) / down (0) from the chat UI |

## Useful filters (Runs view)

* all chat turns: `has(tags, "chat")` (add `eq(is_root, true)`)
* one case: `has(tags, "case:202604032")`
* answers that failed citation checks: Feedback `citation_valid` = 0
* thumbs-down answers: Feedback `user_score` = 0
* tool failures: filter run type = Tool, status = error (or `eq(error, true)`)
* slow turns: sort root runs by Latency; compare per-span latency (retriever vs. model vs. sync)
* the scheduled job: name = `scheduled_kb_refresh`

## Scheduled refresh

`python -m app.cli refresh-all [--scope chatted|all]` (or `REFRESH_CRON_ENABLED=true` for the
in-process scheduler) checks each chatted-about case against the live CRTC record and re-syncs
only the ones with something missing. Each run is traced as `scheduled_kb_refresh`.

## Volume

Per-document download internals are deliberately not traced (the free tier is ~5k traces/month):
one span per sync, one per listing, one per search.
