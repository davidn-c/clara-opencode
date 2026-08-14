"""
Updated facts router for partitioned clara_facts table.

Changes from original:
  - All queries target clara_facts_active explicitly
  - Search includes inactive partition fallback when active returns no results
  - Deactivate endpoint moves row to inactive partition instead of setting active=false
"""

import logging
from fastapi import APIRouter, HTTPException
from app.database import get_pool
from app.embeddings import get_embedding, get_embedding_str
from app.models import FactRequest, FactResponse, FactSearchRequest

logger = logging.getLogger(__name__)
router = APIRouter()

DEDUP_THRESHOLD = 0.88  # cosine similarity — above this, fact is considered duplicate


@router.post("", response_model=FactResponse)
async def write_fact(req: FactRequest):
    pool = await get_pool()

    # Generate embedding for this fact
    try:
        embedding_str = await get_embedding_str(req.fact)
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=503, detail="Embedding service unavailable")

    async with pool.acquire() as conn:
        # Deduplication check — look for semantically similar existing facts
        existing = await conn.fetchrow("""
            SELECT id, fact, confidence,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM clara_facts_active
            WHERE user_id = $2
            AND 1 - (embedding <=> $1::vector) > $3
            ORDER BY similarity DESC
            LIMIT 1
        """, embedding_str, req.user_id, DEDUP_THRESHOLD)

        if existing:
            # Update last_confirmed and potentially raise confidence
            await conn.execute("""
                UPDATE clara_facts_active
                SET last_confirmed = now(),
                    confidence = LEAST(1.0, confidence + 0.05),
                    pinned = pinned OR $2
                WHERE id = $1
            """, existing['id'], req.pinned)

            logger.info(
                f"Fact deduplicated: existing_id={existing['id']} "
                f"similarity={existing['similarity']:.3f}"
            )

            return FactResponse(
                id=existing['id'],
                created_at=existing['id'],
                deduplicated=True,
                existing_id=existing['id']
            )

        # Write new fact to active partition
        row = await conn.fetchrow("""
            INSERT INTO clara_facts_active (
                user_id, category, fact, embedding,
                confidence, implicit, source, source_episode, pinned
            ) VALUES (
                $1, $2, $3, $4::vector,
                $5, $6, $7, $8, $9
            )
            RETURNING id, created_at
        """,
            req.user_id,
            req.category,
            req.fact,
            embedding_str,
            req.confidence,
            req.implicit,
            req.source,
            req.source_episode,
            req.pinned
        )

        logger.info(
            f"Fact written: id={row['id']} "
            f"user={req.user_id} "
            f"category={req.category}"
        )

        return FactResponse(
            id=row['id'],
            created_at=row['created_at'],
            deduplicated=False
        )


@router.post("/search")
async def search_facts(req: FactSearchRequest):
    pool = await get_pool()

    # Generate embedding for search query
    try:
        embedding_str = await get_embedding_str(req.query)
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=503, detail="Embedding service unavailable")

    async with pool.acquire() as conn:
        # Build filters dynamically so param numbering stays correct
        extra_filters = ""
        params = [embedding_str, req.user_id, req.min_confidence, req.limit]

        if req.category:
            params.append(req.category)
            extra_filters += f" AND category = ${len(params)}"

        if req.fact_ids:
            params.append(req.fact_ids)
            extra_filters += f" AND id = ANY(${len(params)}::int[])"

        # Search active partition first
        rows = await conn.fetch(f"""
            SELECT id, category, fact, confidence, source,
                   implicit, last_confirmed,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM clara_facts_active
            WHERE user_id IN ($2, 1)
            AND confidence >= $3
            {extra_filters}
            ORDER BY similarity DESC
            LIMIT $4
        """, *params)

        # If no results from active, fall back to inactive partition
        # (elastic reactivation)
        if not rows:
            rows = await conn.fetch(f"""
                SELECT id, category, fact, confidence, source,
                       implicit, last_confirmed,
                       1 - (embedding <=> $1::vector) AS similarity
                FROM clara_facts_inactive
                WHERE user_id IN ($2, 1)
                AND confidence >= $3
                {extra_filters}
                ORDER BY similarity DESC
                LIMIT $4
            """, *params)

            if rows:
                logger.info(
                    f"Fact search: user={req.user_id} "
                    f"query='{req.query[:50]}' "
                    f"results={len(rows)} (from inactive partition)"
                )
                # Reactivate these facts: move from inactive to active partition
                ids = [r['id'] for r in rows]
                await conn.execute("""
                    INSERT INTO clara_facts_active (
                        id, user_id, category, fact, embedding, confidence,
                        implicit, source, source_episode, pinned,
                        last_confirmed, created_at, updated_at, superseded_by, active
                    )
                    SELECT id, user_id, category, fact, embedding, confidence,
                           implicit, source, source_episode, pinned,
                           last_confirmed, created_at, updated_at, superseded_by, TRUE
                    FROM clara_facts_inactive
                    WHERE id = ANY($1::bigint[])
                """, ids)
                await conn.execute("""
                    DELETE FROM clara_facts_inactive
                    WHERE id = ANY($1::bigint[])
                """, ids)

        results = [dict(r) for r in rows]
        logger.info(
            f"Fact search: user={req.user_id} "
            f"query='{req.query[:50]}' "
            f"results={len(results)}"
        )

        return results


@router.post("/{fact_id}/confirm")
async def confirm_fact(fact_id: int):
    """Reset decay clock on a fact — called when Clara uses it and it proves correct."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE clara_facts_active
            SET last_confirmed = now(),
                confidence = LEAST(1.0, confidence + 0.02)
            WHERE id = $1
        """, fact_id)

        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="Fact not found or inactive")

        logger.info(f"Fact confirmed: id={fact_id}")
        return {"status": "ok", "fact_id": fact_id}


@router.post("/{fact_id}/deactivate")
async def deactivate_fact(fact_id: int, reason: str = None, superseded_by: int = None):
    """
    Move a fact from active to inactive partition instead of soft-deleting.
    Keeps history for audit. The row physically moves to the inactive partition.
    superseded_by optionally records the id of the fact that replaced this
    one, giving a queryable correction lineage.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Extract the row from active partition
        row = await conn.fetchrow("""
            SELECT id, user_id, category, fact, embedding, confidence,
                   implicit, source, source_episode, pinned,
                   last_confirmed, created_at, updated_at
            FROM clara_facts_active
            WHERE id = $1
        """, fact_id)

        if not row:
            raise HTTPException(status_code=404, detail="Fact not found or already inactive")

        # Insert into inactive partition (set active=FALSE to satisfy partition constraint)
        await conn.execute("""
            INSERT INTO clara_facts_inactive (
                id, user_id, category, fact, embedding, confidence,
                implicit, source, source_episode, pinned,
                last_confirmed, created_at, updated_at, superseded_by, active
            ) VALUES (
                $1, $2, $3, $4, $5::vector, $6,
                $7, $8, $9, $10,
                $11, $12, $13, $14, FALSE
            )
        """,
            row['id'], row['user_id'], row['category'], row['fact'],
            row['embedding'], row['confidence'], row['implicit'],
            row['source'], row['source_episode'], row['pinned'],
            row['last_confirmed'], row['created_at'], row['updated_at'],
            superseded_by
        )

        # Delete from active partition
        await conn.execute("DELETE FROM clara_facts_active WHERE id = $1", fact_id)

        logger.info(
            f"Fact deactivated: id={fact_id} reason={reason!r} "
            f"superseded_by={superseded_by} fact=\"{row['fact'][:60]}\""
        )
        return {"status": "ok", "fact_id": row['id'], "deactivated_fact": row['fact']}


@router.get("/user/{user_id}")
async def get_user_facts(user_id: int, category: str = None, limit: int = 50):
    """Retrieve active facts for a user, optionally filtered by category."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if category:
            rows = await conn.fetch("""
                SELECT id, category, fact, confidence,
                       source, implicit, last_confirmed
                FROM clara_facts_active
                WHERE user_id IN ($1, 1)
                AND category = $2
                ORDER BY confidence DESC
                LIMIT $3
            """, user_id, category, limit)
        else:
            rows = await conn.fetch("""
                SELECT id, category, fact, confidence,
                       source, implicit, last_confirmed
                FROM clara_facts_active
                WHERE user_id IN ($1, 1)
                ORDER BY confidence DESC
                LIMIT $2
            """, user_id, limit)

        return [dict(r) for r in rows]
