-- Every stored vector carries vec_hash, an md5 of its value, indexed per (model, strategy).
-- Search completes a group of exactly tied chunks at the HNSW pool's edge (core/search.py,
-- _ann): it looks up the chunks whose vector equals a tied one by this hash, instead of
-- scoring every chunk of the corpus (about 260 ms on live's 62k repo-doc chunks; deviation
-- d18). vector::text prints each float32 so that it reads back to the same value, so equal
-- hashes mean equal vectors (an md5 collision aside).
ALTER TABLE embeddings
  ADD COLUMN vec_hash uuid GENERATED ALWAYS AS (md5(vec::text)::uuid) STORED;
ALTER TABLE repodoc_embeddings
  ADD COLUMN vec_hash uuid GENERATED ALWAYS AS (md5(vec::text)::uuid) STORED;
CREATE INDEX embeddings_vec_hash ON embeddings (model_id, strategy, vec_hash);
CREATE INDEX repodoc_embeddings_vec_hash ON repodoc_embeddings (model_id, strategy, vec_hash);
