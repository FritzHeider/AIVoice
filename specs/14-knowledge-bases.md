# 14 — Knowledge Bases (`packages/va_kb`)

## 1. Package layout

```
packages/va_kb/src/va_kb/
├── models.py        # KnowledgeBase, KbDocument, Chunk, RetrievalHit
├── parsers/         # pdf.py docx.py text.py html.py csv_.py  (each: async parse(bytes, name) -> ParsedDoc)
├── crawler.py       # sitemap discovery + polite crawler
├── chunker.py       # heading-aware token chunker
├── embed.py         # batching over va_llm.embeddings
├── ingest.py        # activities used by KnowledgeIngestWorkflow
├── retrieve.py      # hybrid retrieval
├── chat.py          # KB chat answer synthesis
└── format.py        # prompt injection formatting
```

## 2. API (envelope format)

| Method | Path | Body | Response `data` |
|---|---|---|---|
| GET | `/v1/knowledge` | `type?` (`FILE`/`WEB_SCRAPE`/`TEXT`), `status?`, `limit`, `cursor` | `[KnowledgeBase]` |
| POST | `/v1/knowledge` | `{"name","description?"}` | `KnowledgeBase` (empty, status `COMPLETED`) |
| GET | `/v1/knowledge/{kb_id}` | — | `KnowledgeBase` + `documents` (first 100) |
| PATCH | `/v1/knowledge/{kb_id}` | `{"name?","description?"}` | `KnowledgeBase` |
| DELETE | `/v1/knowledge/{kb_id}` | — | `{"deleted":true}` (soft delete; chunks hard-deleted by worker) |
| POST | `/v1/knowledge/upload` | multipart: `file` (one or many), `kb_id?`, `name?`, `description?` | `{"kb": KnowledgeBase, "documents":[KbDocument]}` — creates KB when `kb_id` absent |
| POST | `/v1/knowledge/text` | `{"kb_id?","name","text"}` (text ≤ 2 MB) | same as upload |
| POST | `/v1/knowledge/crawl/discover` | `{"url"}` | `{"urls":[{"url","lastmod?"}],"source":"sitemap"|"links","truncated":bool}` (≤ 500) |
| POST | `/v1/knowledge/crawl` | `{"kb_id?","name?","urls":[…]}` or `{"kb_id?","name?","start_url","max_pages?≤500","max_depth?≤3","include_patterns?":[],"exclude_patterns?":[]}` | `{"kb": KnowledgeBase, "documents":[KbDocument]}` |
| GET | `/v1/knowledge/{kb_id}/documents` | `limit`, `cursor` | `[KbDocument]` |
| DELETE | `/v1/knowledge/{kb_id}/documents/{doc_id}` | — | `{"deleted":true}` |
| POST | `/v1/knowledge/{kb_id}/documents/{doc_id}/refresh` | — | re-fetch WEB_SCRAPE doc / re-process |
| POST | `/v1/knowledge/{kb_id}/chat` | `{"message","history?":[{role,content}],"top_k?":5}` | `{"answer","sources":[{"document_id","name","source_url","page_number","excerpt","score"}]}` |
| POST | `/v1/knowledge/{kb_id}/search` | `{"query","top_k?":5}` | `[RetrievalHit]` (debug/testing) |

Public id format `KB-<uuid>` everywhere (path params accept both forms).

`KnowledgeBase` JSON: `{"id":"KB-…","name","description","type":"FILE"|"WEB_SCRAPE"|"TEXT"|"MIXED","status","document_count","chunk_count","created_at","updated_at"}`
(`type` derived: single type of all docs or `MIXED`). `KbDocument`: `{"id","kb_id","type","name","source_url","mime_type","size_bytes","status","error","token_count","created_at"}`.

**Legacy compat** `/v1/knowledgebases` (Bland "vector stores"): `POST /v1/knowledgebases {name, description, text}`
→ `{"vector_id": "KB-…"}`; `GET /v1/knowledgebases` → `{"vectors":[{"vector_id","name","description"}]}`;
`GET /v1/knowledgebases/{id}`; `POST /v1/knowledgebases/{id}` update text (replaces the single TEXT doc);
`POST /v1/knowledgebases/upload` (multipart `file`, `name`, `description`) → `{"vector_id"}`; `DELETE`. These use the
legacy envelope.

Upload rules: extensions/MIME `pdf`, `txt`, `md`, `doc` (rejected with `KB_UNSUPPORTED_FILE` message "Legacy .doc is
not supported; save as .docx"), `docx`, `html`/`htm`, `csv`; max 50 MB per file (`KB_FILE_TOO_LARGE`), max 20 files per
request. Files stored at `s3://{S3_BUCKET_KB}/{org_id}/{kb_id}/{doc_id}/{sanitized_name}`.

## 3. Ingestion: `KnowledgeIngestWorkflow(kb_id, document_ids)` (task queue `va-ingest`)

Per document (parallel, max 4 per workflow):
1. `fetch_document`: FILE/TEXT read from S3; WEB_SCRAPE fetch via crawler (§5) with SSRF guard → store raw HTML in S3.
2. `parse_document` (thread pool): returns `ParsedDoc{sections:[{heading_path, text, page_number?, source_url?}]}`.
   Compute `content_sha256` of normalized text; if another COMPLETED doc in the same KB has the same hash → mark
   doc COMPLETED with `token_count` and skip chunks (dedupe).
3. `chunk_document` (§6).
4. `embed_chunks` (§6.3) in batches.
5. `store_chunks`: delete old chunks for the document then bulk insert (COPY via asyncpg) in one transaction.
6. Update document status, token_count; KB counters; KB status = `PROCESSING` while any doc processing,
   `FAILED` if all docs failed, else `COMPLETED`.
7. Emit `kb.ready` / `kb.failed`; usage event `kb_embedding_tokens`.
Activity retries: 3 with exponential backoff; parse errors are non-retryable (doc → FAILED with `error`).

## 4. Parsers

| Type | Library | Rules |
|---|---|---|
| PDF | `pypdf` | Per page text; page number kept; lines joined, hyphenation at line end repaired (`-\n` + lowercase → join); pages with < 20 chars of text flagged; if > 80 % pages empty → FAILED "PDF appears to be scanned; OCR not supported". Encrypted PDFs: try empty password, else FAILED. |
| DOCX | `python-docx` | Paragraph styles `Heading 1..6` build heading path; tables rendered as `col1 | col2` rows. |
| TXT/MD | builtin | UTF-8 (fallback cp1252); Markdown `#` headings build heading path. |
| HTML | `trafilatura.extract(output_format="markdown", include_tables=True)`; fallback `selectolax` text of `<main>`/`<article>`/`<body>` | Title from `<title>`. |
| CSV | `csv` module | Header row; each row rendered `"header1: v1; header2: v2"`; groups of rows up to chunk size; max 100 000 rows. |

## 5. Crawler

- Discovery: fetch `{origin}/robots.txt` (parse `Sitemap:` lines and rules with `urllib.robotparser`), then
  `/sitemap.xml`, `/sitemap_index.xml`; parse with `selectolax`/`xml.etree` (nested indexes up to depth 2); fallback:
  fetch start URL and collect same-origin `<a href>` links.
- Crawl: BFS from start URL; same registrable domain only (`tldextract`-free rule: same host or subdomain of start
  host); respects robots.txt disallow for user agent `VoiceAgentsBot`; max pages 500, max depth 3, rate 2 requests/s
  per host, timeout 10 s per page, max page size 5 MB, only `text/html`/`text/plain`; canonical URL normalization
  (strip fragment, sort query, drop `utm_*`); include/exclude glob patterns.
- User-Agent `VoiceAgentsBot/1.0 (+{PUBLIC_API_BASE_URL}/bot)`.
- Each fetched page becomes one `kb_documents` row (`type='WEB_SCRAPE'`, `source_url`).

## 6. Chunking and embeddings

### 6.1 Tokenizer
`tiktoken.get_encoding("cl100k_base")` for counting (approximation for the embedding model).

### 6.2 Algorithm (`chunk_document(sections, size=400, overlap=60)`)
1. For each section, split text into paragraphs (blank lines); paragraphs > size tokens split into sentences
   (regex `(?<=[.!?])\s+(?=[A-Z0-9"“(])`), sentences > size split by tokens.
2. Greedily pack units into chunks up to `size` tokens, never crossing a section boundary unless the section is
   < 50 tokens (then merged with the next section, heading path = first).
3. Overlap: each new chunk starts with the last `overlap` tokens' worth of whole sentences of the previous chunk in
   the same section.
4. Chunk text stored as `"{heading_path joined ' > '}\n\n{text}"` when a heading path exists.
5. `ordinal` sequential per document; `page_number` of first unit; `source_url` from section.
KB-level `chunk_size_tokens`/`chunk_overlap_tokens` override defaults (bounds 100–1 200 / 0–300).

### 6.3 Embeddings
`va_llm.embeddings.embed(texts, model=EMBEDDINGS_MODEL, dimensions=EMBEDDINGS_DIMENSIONS)` in batches of 64 texts
(≤ 8 000 tokens each, truncated) with concurrency 4; retries on 429/5xx with backoff (1 s, 2 s, 4 s, 8 s). Vectors
L2-normalized before storage. KB records model + dimensions; retrieval refuses (500 `KB_ERROR`) if they differ from
current settings (re-embed job `POST /internal/kb/{id}/reembed` exists for migrations).

## 7. Retrieval (`retrieve(org_id, kb_ids, query, top_k=5)`)

1. Embed query (cache Redis `kbq:{sha256(model+query)}` 1 h).
2. SQL (tenant session):
```sql
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> :qvec) AS rnk
  FROM kb_chunks WHERE kb_id = ANY(:kb_ids)
  ORDER BY embedding <=> :qvec LIMIT 40
), kw AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, plainto_tsquery('simple', :q)) DESC) AS rnk
  FROM kb_chunks WHERE kb_id = ANY(:kb_ids) AND tsv @@ plainto_tsquery('simple', :q)
  ORDER BY ts_rank_cd(tsv, plainto_tsquery('simple', :q)) DESC LIMIT 40
), fused AS (
  SELECT id, sum(1.0 / (60 + rnk)) AS score FROM (
    SELECT id, rnk FROM vec UNION ALL SELECT id, rnk FROM kw) u GROUP BY id
)
SELECT c.id, c.document_id, c.content, c.source_url, c.page_number, c.heading_path, d.name, f.score
FROM fused f JOIN kb_chunks c ON c.id = f.id JOIN kb_documents d ON d.id = c.document_id
ORDER BY f.score DESC LIMIT :top_k;
```
   Session sets `SET LOCAL hnsw.ef_search = 80`.
3. Per-call cache: `call:{id}:kbcache` hash keyed by `sha256(kb_ids+normalized query)` (TTL 2 h).
4. Latency budget: 120 ms p95 (embedding ~60 ms + SQL ~30 ms); on timeout (250 ms) the turn proceeds without KB
   context and emits `call.error` warning `KB_TIMEOUT`.
5. Query construction in voice: last user message; if < 4 words, prepend previous assistant message.

### 7.1 Prompt injection format (verbatim)
```
<knowledge>
Use the following reference information to answer if relevant. If it does not contain the answer, say you are
not sure rather than inventing details. Do not mention that you are reading from documents.
[1] (source: {document_name}{, page {page_number}}) {chunk content}
[2] …
</knowledge>
```
Each chunk truncated to 1 200 characters; total block ≤ 6 000 characters.

## 8. KB chat (`POST /v1/knowledge/{kb_id}/chat`)
Retrieve top_k, then analysis-model completion (non-streaming) with system prompt (verbatim):
```
You answer questions using only the provided knowledge excerpts. Cite excerpts inline as [n]. If the excerpts do
not contain the answer, reply exactly: "I couldn't find that in the knowledge base." Be concise.
```
User message: `<knowledge>…</knowledge>` block (§7.1) + `\n\nQuestion: {message}`; `history` (≤ 10 messages) placed
before. Response `answer` + `sources` for chunks actually cited (`[n]` parsed); if none cited, all retrieved sources.
Usage: LLM tokens metered (`llm_tokens_in/out`).

## 9. Limits per plan

| Plan | KBs | Documents per KB | Total chunks per org | Crawl pages per request |
|---|---|---|---|---|
| start | 5 | 50 | 20 000 | 100 |
| build | 50 | 500 | 500 000 | 500 |
| scale | 200 | 2 000 | 2 000 000 | 500 |
| enterprise | unlimited | unlimited | unlimited (configurable) | 500 |
Exceeding → 403 `FEATURE_NOT_IN_PLAN` with `details.limit`.

## 10. Tests required
- Each parser with fixture files generated in-test (pypdf writer, python-docx writer, CSV, HTML).
- Scanned-PDF detection, encrypted PDF, legacy .doc rejection, size limits.
- Crawler against a local aiohttp/starlette test site: robots disallow, depth, max pages, rate limiting (fake clock),
  sitemap index nesting, canonicalization, SSRF block.
- Chunker invariants (hypothesis): no chunk > size+overlap tokens; coverage of all text; deterministic output.
- Embedding batching/retry with fake LLM server.
- Retrieval: hybrid fusion ranking on a crafted corpus (keyword-only match, vector-only match, both); tenant isolation;
  per-call cache; timeout path.
- KB chat citation parsing; legacy endpoints envelope.
- Workflow: ingestion state transitions, dedupe, partial failure KB status.

## 11. Acceptance criteria
- 100-page PDF ingests in < 60 s in CI with simulator embeddings.
- Retrieval p95 < 120 ms on 100 000 chunks (load test fixture) with HNSW index.
- Voice call through a Knowledge Base node answers from a fixture document in simulator e2e.
