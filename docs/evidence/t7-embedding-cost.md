# t7 embedding cost measurement

Measured 2026-10-24 on the DGX host against the lobes gateway
(`http://127.0.0.1:8001`, `POST /v1/embeddings`), using the real
`culture_library.gateway.GatewayEmbedder` and `culture_library.core.search.chunk_text`
(1000-char paragraph-packed chunks). The API key came from the shell environment only and was
not written anywhere.

## Sample

- Source: `docs/specs/*.md` of the 60 PUBLIC repos under `~/git` that have one
  (visibility checked with `gh repo view --json isPrivate`): 587 files, 13,788 chunks.
- Chunking stats: mean 23.5 chunks per file (median 19).
- Embedded sample: 200 chunks drawn at random (seed 7), 168,891 chars, mean 844 chars per chunk.

## Results

| Model | Dim | Batch | 200 chunks | Throughput |
|-------|-----|-------|-----------|------------|
| `gemma2-embed` | 768 | 16 | 9.12 s | 21.9 chunks/s |
| `gemma2-embed` | 768 | 32 | 15.17 s | 13.2 chunks/s |
| `Qwen/Qwen3-Embedding-0.6B` | 1024 | 16 | 7.95 s | 25.2 chunks/s |
| `Qwen/Qwen3-Embedding-0.6B` | 1024 | 32 | 6.73 s | 29.7 chunks/s |

Single runs, no repetition: the gemma2 batch-32 figure is noisy (the model is served by a
remote peer, `orin`), so treat throughput as roughly 13-22 chunks/s for `gemma2-embed`.

Reliability observation: during this session `gemma2-embed` returned HTTP 503
(`backend_unavailable`, peer unreachable) on every request for several minutes, then recovered.
That is exactly the outage the indexer's retry/backoff path is built for; the local
Qwen3 embedder did not fail.

## Extrapolation (assumptions marked)

- ~2,950 records: ASSUMES each record looks like a spec (23.5 chunks) -> about 69,300 chunks.
  At 21.9 chunks/s that is about 53 minutes (at 13.2/s about 88 minutes; Qwen3 at 25-30/s about
  38-46 minutes).
- The 7.4 MB book: ASSUMES about 1 byte per char of plain text -> about 8,760 chunks of
  844 chars. At 21.9 chunks/s about 6.7 minutes (13.2/s: 11 minutes).
- Both are one-off backfill costs run in the background; steady-state uploads add seconds.

## Caveats

- The real corpus (the 2,950 records and the book) was not available to this task, so the
  sample is a proxy; record count, chunks per record and text density are assumptions above.
- Retrieval quality of the two models was not evaluated; only cost and availability.
- Default model is `gemma2-embed` (override with `LOBES_EMBED_MODEL`).
