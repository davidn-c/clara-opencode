"""
Updated sessions router for partitioned clara_facts table.

Changes from original:
  - Fact query targets clara_facts_active explicitly
  - Episode query targets clara_episodes (unchanged)
"""

import logging
from fastapi import APIRouter, HTTPException
from app.database import get_pool
from app.models import SessionStartRequest, SessionStartResponse, SessionEndRequest
from uuid import UUID
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/start", response_model=SessionStartResponse)
async def start_session(req: SessionStartRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check for unclean prior shutdown
        unclean = await conn.fetchrow("""
            SELECT id FROM clara_sessions
            WHERE instance = $1
            AND clean_shutdown = FALSE
            AND ended_at IS NULL
        """, req.instance)
        if unclean:
            logger.warning(f"Unclean shutdown detected for {req.instance}, session {unclean['id']}")
            await conn.execute("""
                UPDATE clara_sessions
                SET ended_at = now(), clean_shutdown = FALSE
                WHERE id = $1
            """, unclean['id'])

        # Create new session
        session_id = await conn.fetchval("""
            INSERT INTO clara_sessions (user_id, instance)
            VALUES ($1, $2)
            RETURNING id
        """, req.user_id, req.instance)

        # Load active persona
        persona_row = await conn.fetchrow("""
            SELECT persona, communication_style
            FROM clara_self_model
            WHERE active = TRUE
            LIMIT 1
        """)

        # Load active facts for this user and shared (from active partition only)
        facts = await conn.fetch("""
            SELECT id, category, fact, confidence, source, pinned
            FROM clara_facts_active
            WHERE user_id IN ($1, 1)
            AND (pinned = TRUE OR confidence > 0.1)
            ORDER BY pinned DESC, confidence DESC
            LIMIT 60
        """, req.user_id)

        # Load recent episodes — mix of high importance and recent
        episodes = await conn.fetch("""
            (
                SELECT speaker, content, occurred_at, emotional_tone, importance
                FROM clara_episodes
                WHERE user_id = $1
                AND importance >= 0.4
                ORDER BY occurred_at DESC
                LIMIT 20
            )
            UNION
            (
                SELECT speaker, content, occurred_at, emotional_tone, importance
                FROM clara_episodes
                WHERE user_id = $1
                ORDER BY occurred_at DESC
                LIMIT 10
            )
            ORDER BY occurred_at DESC
        """, req.user_id)

        # Load due reminders
        reminders = await conn.fetch("""
            SELECT id, title, detail, due_at, priority
            FROM clara_reminders
            WHERE user_id IN ($1, 1)
            AND completed_at IS NULL
            AND (due_at IS NULL OR due_at <= now() + interval '24 hours')
            AND (snoozed_until IS NULL OR snoozed_until <= now())
            ORDER BY priority DESC, due_at ASC
        """, req.user_id)

        # Load integration states
        integrations = await conn.fetch("""
            SELECT system, device, state, reachable, last_verified
            FROM clara_integration_state
            ORDER BY system, device
        """)

        logger.info(f"Session started: {session_id} for user {req.user_id} on {req.instance}")

        return SessionStartResponse(
            session_id=session_id,
            persona=persona_row['persona'] if persona_row else '',
            communication_style=persona_row['communication_style'] if persona_row else None,
            facts=[dict(f) for f in facts],
            recent_episodes=[dict(e) for e in episodes],
            due_reminders=[dict(r) for r in reminders],
            integrations=[dict(i) for i in integrations]
        )


@router.post("/end")
async def end_session(req: SessionEndRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE clara_sessions
            SET ended_at = now(),
                clean_shutdown = TRUE,
                context_pct = $2,
                notes = $3
            WHERE id = $1
        """, req.session_id, req.context_pct, req.notes)

        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="Session not found")

        logger.info(f"Session ended cleanly: {req.session_id}")
        return {"status": "ok", "session_id": str(req.session_id)}
