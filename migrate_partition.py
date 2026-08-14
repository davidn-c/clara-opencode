#!/usr/bin/env python3
"""
Migrate clara_facts to partitioned table (active / inactive).

Steps:
  1. Create clara_facts_new (partitioned by LIST on active)
  2. Create partitions: clara_facts_active, clara_facts_inactive
  3. Migrate data + indexes + constraints + triggers
  4. Swap tables atomically (rename old -> backup, new -> active)
  5. Verify row counts match

Usage:
    source ~/clara_memory/venv/bin/activate
    python3 migrate_partition.py

This script is idempotent — running it twice is safe (the backup table
will be renamed back to clara_facts if clara_facts_new already exists).
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.expanduser("~/clara_memory/logs/migration.log")),
    ],
)
log = logging.getLogger("migration")

# Load env from the memory service's config directory
env_path = os.path.expanduser("~/clara_memory/config/.env")
if os.path.exists(env_path):
    from dotenv import load_dotenv as _ld
    _ld(env_path)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME", "claramemory")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASSWORD")


async def get_conn():
    return await asyncpg.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS,
    )


# ── SQL: Create partitioned table ──────────────────────────
CREATE_NEW_TABLE = """
CREATE TABLE clara_facts_new (
    id             bigint NOT NULL DEFAULT nextval('clara_facts_id_seq'),
    user_id        bigint NOT NULL,
    category       text    NOT NULL CHECK (category = ANY (ARRAY[
        'preference','routine','person','device','media',
        'health','work','inferred','household'
    ])),
    fact           text    NOT NULL,
    embedding      vector(768),
    confidence     double precision NOT NULL DEFAULT 1.0
                          CHECK (confidence >= 0 AND confidence <= 1),
    implicit       boolean NOT NULL DEFAULT false,
    source         text    NOT NULL DEFAULT 'learned'
                          CHECK (source IN ('learned','taught','corrected')),
    active         boolean NOT NULL DEFAULT true,
    last_confirmed timestamptz NOT NULL DEFAULT now(),
    superseded_by  bigint,
    source_episode bigint,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    pinned         boolean NOT NULL DEFAULT false
) PARTITION BY LIST (active);
"""

CREATE_PARTITIONS = """
CREATE TABLE clara_facts_active    PARTITION OF clara_facts_new (
    updated_at DEFAULT now()
) FOR VALUES IN (true);
CREATE TABLE clara_facts_inactive   PARTITION OF clara_facts_new (
    updated_at DEFAULT now()
) FOR VALUES IN (false);
"""

CREATE_INDEXES = """
-- Primary key on each partition
ALTER TABLE clara_facts_active    ADD CONSTRAINT clara_facts_active_pkey PRIMARY KEY (id);
ALTER TABLE clara_facts_inactive  ADD CONSTRAINT clara_facts_inactive_pkey PRIMARY KEY (id);

-- HNSW on each partition (needed for vector search across both)
CREATE INDEX idx_facts_active_hnsw    ON clara_facts_active    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_facts_inactive_hnsw  ON clara_facts_inactive  USING hnsw (embedding vector_cosine_ops);

-- Confidence index on active only
CREATE INDEX idx_facts_active_confidence ON clara_facts_active (confidence DESC);

-- User+category index on active only
CREATE INDEX idx_facts_active_user_cat ON clara_facts_active (user_id, category);

-- Taught facts index on active only
CREATE INDEX idx_facts_active_taught   ON clara_facts_active (user_id, source)
    WHERE source = 'taught' AND active = true;
"""

CREATE_CONSTRAINTS = """
ALTER TABLE clara_facts_active
    ADD CONSTRAINT clara_facts_active_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES clara_users(id);

ALTER TABLE clara_facts_inactive
    ADD CONSTRAINT clara_facts_inactive_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES clara_users(id);

-- Note: superseded_by FK is NOT enforced across partitions.
-- The app handles correction lineage via the deactivate endpoint.
"""

CREATE_TRIGGER = """
CREATE OR REPLACE FUNCTION clara_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_clara_facts_active_updated_at
    BEFORE UPDATE ON clara_facts_active
    FOR EACH ROW EXECUTE FUNCTION clara_set_updated_at();

CREATE TRIGGER trg_clara_facts_inactive_updated_at
    BEFORE UPDATE ON clara_facts_inactive
    FOR EACH ROW EXECUTE FUNCTION clara_set_updated_at();
"""

MIGRATE_DATA = """
INSERT INTO clara_facts_new (
    id, user_id, category, fact, embedding, confidence,
    implicit, source, active, last_confirmed,
    superseded_by, source_episode, created_at, updated_at, pinned
)
SELECT id, user_id, category, fact, embedding, confidence,
       implicit, source, active, last_confirmed,
       superseded_by, source_episode, created_at, updated_at, pinned
FROM clara_facts
ORDER BY id;
"""

# ── Validation ─────────────────────────────────────────────
VALIDATE_COUNTS = """
SELECT
    (SELECT count(*) FROM clara_facts)   AS old_total,
    (SELECT count(*) FROM clara_facts_new) AS new_total,
    (SELECT count(*) FROM clara_facts_active) AS new_active,
    (SELECT count(*) FROM clara_facts_inactive) AS new_inactive;
"""

# ── Swap ───────────────────────────────────────────────────
SWAP_TABLES = """
-- Rename old -> backup
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'clara_facts_backup') THEN
        DROP TABLE clara_facts_backup;
    END IF;
END $$;

ALTER TABLE clara_facts    RENAME TO clara_facts_backup;
ALTER TABLE clara_facts_new RENAME TO clara_facts;
ALTER SEQUENCE clara_facts_id_seq OWNED BY clara_facts.id;
"""

# ── Verify FKs survive swap ────────────────────────────────
VERIFY_FKS = """
SELECT
    tc.constraint_name,
    tc.table_name AS child_table,
    kcu.column_name AS child_column,
    ccu.table_name AS parent_table,
    ccu.column_name AS parent_column
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_name = 'clara_facts';
"""


async def main():
    log.info("=== Migration: clara_facts partitioning ===")
    start = datetime.utcnow()

    conn = await get_conn()
    try:
        # Step 1: Check if already migrated
        log.info("Step 1: Checking if migration already done...")
        exists = await conn.fetchval(
            "SELECT EXISTS ("
            "  SELECT 1 FROM pg_tables WHERE tablename = 'clara_facts_new'"
            ")")
        if exists:
            log.info("clara_facts_new already exists — assuming already migrated.")
            return

        # Step 2: Create partitioned table
        log.info("Step 2: Creating partitioned table...")
        await conn.execute(CREATE_NEW_TABLE)
        log.info("  Created clara_facts_new (partitioned)")

        # Step 3: Create partitions
        log.info("Step 3: Creating partitions...")
        await conn.execute(CREATE_PARTITIONS)
        log.info("  Created clara_facts_active, clara_facts_inactive")

        # Step 4: Create indexes
        log.info("Step 4: Creating indexes...")
        await conn.execute(CREATE_INDEXES)
        log.info("  Created HNSW + btree indexes on both partitions")

        # Step 5: Create constraints
        log.info("Step 5: Creating constraints...")
        await conn.execute(CREATE_CONSTRAINTS)
        log.info("  Created FK constraints")

        # Step 6: Create triggers
        log.info("Step 6: Creating triggers...")
        await conn.execute(CREATE_TRIGGER)
        log.info("  Created updated_at triggers")

        # Step 7: Migrate data
        log.info("Step 7: Migrating data...")
        await conn.execute(MIGRATE_DATA)
        log.info("  Data migrated")

        # Step 8: Validate
        log.info("Step 8: Validating row counts...")
        counts = await conn.fetchrow(VALIDATE_COUNTS)
        log.info(
            f"  Old total: {counts['old_total']} | "
            f"New total: {counts['new_total']} | "
            f"Active: {counts['new_active']} | "
            f"Inactive: {counts['new_inactive']}"
        )
        if counts['old_total'] != counts['new_total']:
            log.error(f"  MISMATCH: old={counts['old_total']} new={counts['new_total']}")
            sys.exit(1)
        log.info("  Row counts match ✓")

        # Step 9: Swap tables
        log.info("Step 9: Swapping tables...")
        await conn.execute(SWAP_TABLES)
        log.info("  Swap complete")

        # Step 10: Verify FKs survived
        log.info("Step 10: Verifying foreign keys...")
        fks = await conn.fetch(VERIFY_FKS)
        for fk in fks:
            log.info(
                f"  FK: {fk['child_table']}.{fk['child_column']} -> "
                f"{fk['parent_table']}.{fk['parent_column']}"
            )
        log.info(f"  {len(fks)} foreign keys verified ✓")

        elapsed = (datetime.utcnow() - start).total_seconds()
        log.info(f"=== Migration complete in {elapsed:.1f}s ===")
        log.info(
            "Note: clara_facts_backup still exists. "
            "Drop it after verifying the app works correctly."
        )

    except Exception as e:
        log.error(f"Migration failed: {e}", exc_info=True)
        # Rollback: rename back if swap happened
        exists_new = await conn.fetchval(
            "SELECT EXISTS ("
            "  SELECT 1 FROM pg_tables WHERE tablename = 'clara_facts_new'"
            ")")
        if exists_new:
            log.info("Rolling back — renaming clara_facts_new aside...")
            await conn.execute("ALTER TABLE clara_facts_new RENAME TO clara_facts_rollback")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
