-- Plan 018 WI-1.2: note entity convergence. Adds the column the write-through
-- path needs on the local memories projection. regista is the authority for
-- note entities (memories + reflections); this table is a search/read
-- projection (pgvector embeddings, legacy reads/search), exactly as
-- work_items is a projection of regista work-items (schema 810).

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS regista_note_id UUID,
    ADD COLUMN IF NOT EXISTS pending_sync BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_memories_regista_note_id
    ON memories (regista_note_id)
    WHERE regista_note_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_pending_sync
    ON memories (project_id, pending_sync)
    WHERE pending_sync = TRUE;
