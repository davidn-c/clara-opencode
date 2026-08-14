"""
core/memory_client.py — MemoryClient: long-term memory system wrapper.

Extracted from clara2.py to decouple memory functionality from the
monolithic script. Handles facts, episodes, sessions, and memory
corrections via a local memory service.
"""

import httpx

from core.services import MEMORY_BASE, USER_ID, INSTANCE


class MemoryClient:
    """Client for the local memory service (facts, episodes, sessions)."""

    def __init__(self, base_url: str = MEMORY_BASE):
        self.base = base_url
        self.session_id = None
        self.client = httpx.Client(timeout=10.0)
        # Facts actually surfaced to Dave this session (via RECALL:
        # results), populated by recall_memory(). Scopes its match search
        # to this pool instead of the whole facts table, so a correction
        # can't accidentally deactivate an unrelated same-subject fact
        # Clara never showed him. Reset each session in start_session().
        self.shown_fact_ids: set[int] = set()

    def start_session(self) -> dict:
        """Start a new memory session. Returns session data."""
        resp = self.client.post(f"{self.base}/session/start", json={
            "user_id": USER_ID,
            "instance": INSTANCE
        })
        resp.raise_for_status()
        data = resp.json()
        self.session_id = data["session_id"]
        self.shown_fact_ids = set()
        return data

    def end_session(self):
        """End the current memory session."""
        if not self.session_id:
            return
        try:
            self.client.post(f"{self.base}/session/end", json={
                "session_id": self.session_id,
                "instance": INSTANCE
            })
        except Exception:
            pass

    def write_episode(self, speaker: str, content: str, importance: float = 0.5):
        """Write a conversation episode to the current session."""
        if not self.session_id:
            return
        try:
            self.client.post(f"{self.base}/episode", json={
                "user_id": USER_ID,
                "session_id": self.session_id,
                "instance": INSTANCE,
                "speaker": speaker,
                "content": content,
                "importance": importance
            })
        except Exception as e:
            print(f"[memory] episode write failed: {e}")

    def write_fact(self, category: str, fact: str, confidence: float = 0.9,
                   implicit: bool = False, pinned: bool = False,
                   source: str = "learned") -> int | None:
        """Write a fact to long-term memory.

        Parameters
        ----------
        category : str
            Fact category (preference, routine, person, device, media,
            health, work, inferred, household).
        fact : str
            The fact content.
        confidence : float, optional
            Confidence score 0.0-1.0. Defaults to 0.9.
        implicit : bool, optional
            True if this fact was inferred/derived (not directly stated).
            Implicit facts are treated as speculative and weighted lower
            during retrieval and correction matching. Defaults to False.
        pinned : bool, optional
            True to pin this fact (always included in recall). Defaults to False.
        source : str, optional
            Source type: "learned", "taught", or "corrected". Defaults to "learned".

        Returns
        -------
        int | None
            The fact's id on success, or None on failure.
        Truthy/falsy behavior is preserved for existing `if ok:` call sites;
        correction logic additionally needs the id to set superseded_by on
        retired ancestors.
        """
        if not self.session_id:
            return None
        try:
            resp = self.client.post(f"{self.base}/facts", json={
                "user_id": USER_ID,
                "category": category,
                "fact": fact,
                "confidence": confidence,
                "implicit": implicit,
                "source": source,
                "pinned": pinned
            })
            resp.raise_for_status()
            data = resp.json()
            return data.get("id") or data.get("existing_id")
        except Exception as e:
            print(f"[memory] fact write failed: {e}")
            return None

    def track_mention(self, person_name: str) -> bool:
        """Track a mention of a person by name.
        
        Parameters
        ----------
        person_name : str
            The name of the person mentioned.
            
        Returns
        -------
        bool
            True on success, False on failure.
        """
        try:
            resp = self.client.post(f"{self.base}/mentions", json={
                "user_id": str(USER_ID),
                "person_name": person_name
            })
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[memory] mention tracking failed: {e}")
            return False

    def deactivate_fact(self, fact_id: int, reason: str = None,
                        superseded_by: int = None) -> bool:
        """Deactivate a fact, marking it as superseded or outdated."""
        try:
            params = {}
            if reason:
                params["reason"] = reason
            if superseded_by is not None:
                params["superseded_by"] = superseded_by
            resp = self.client.post(
                f"{self.base}/facts/{fact_id}/deactivate",
                params=params
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[memory] fact deactivate failed: id={fact_id}: {e}")
            return False

    def toggle_pin(self, fact_id: int, pinned: bool = True) -> bool:
        """Toggle the pinned state of an existing fact.

        Parameters
        ----------
        fact_id : int
            The ID of the fact to pin/unpin.
        pinned : bool, optional
            True to pin, False to unpin. Defaults to True.

        Returns
        -------
        bool
            True on success, False on failure.

        Notes
        -----
        Requires the memory service to expose a PATCH /facts/{id}/pin endpoint.
        Implementation of the server-side endpoint is pending.
        """
        try:
            resp = self.client.post(
                f"{self.base}/facts/{fact_id}/pin",
                json={"pinned": pinned}
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[memory] toggle_pin failed: id={fact_id}: {e}")
            return False

    def find_best_fact_match(self, query: str, fact_ids: list[int] | None = None,
                             min_similarity: float = 0.55,
                             prefer_explicit: bool = True) -> dict | None:
        """Find the single best-matching active fact for a correction target.

        Parameters
        ----------
        query : str
            The query string to match against facts.
        fact_ids : list[int] | None, optional
            Scope search to specific fact IDs (e.g., facts shown this session).
        min_similarity : float, optional
            Minimum similarity threshold for explicit facts. Defaults to 0.55.
        prefer_explicit : bool, optional
            If True (default), first search for explicit facts (implicit=False).
            Only falls back to implicit facts if no explicit match is found.
            This is critical for correction logic — you want to correct against
            confirmed facts, not inferences. If False, searches all facts
            regardless of implicit status.

        Returns
        -------
        dict | None
            The best matching fact dict, or None if no match found.
        """
        try:
            # Phase 1: Search for explicit facts if prefer_explicit=True
            if prefer_explicit:
                payload = {
                    "user_id": USER_ID,
                    "query": query,
                    "limit": 1,
                    "implicit": False  # Only explicit facts
                }
                if fact_ids:
                    payload["fact_ids"] = fact_ids
                r = self.client.post(f"{self.base}/facts/search", json=payload)
                r.raise_for_status()
                results = r.json()
                if results and results[0].get("similarity") is not None \
                        and results[0]["similarity"] >= min_similarity:
                    return results[0]

            # Phase 2: Fall back to all facts (explicit + implicit)
            payload = {"user_id": USER_ID, "query": query, "limit": 1}
            if fact_ids:
                payload["fact_ids"] = fact_ids
            r = self.client.post(f"{self.base}/facts/search", json=payload)
            r.raise_for_status()
            results = r.json()
            if results and results[0].get("similarity") is not None \
                    and results[0]["similarity"] >= min_similarity:
                return results[0]
        except Exception as e:
            print(f"[memory] fact match search failed: {e}")
        return None

    def close(self):
        """Close the underlying httpx client."""
        self.client.close()

    def search_facts(self, query: str, limit: int = 3, min_similarity: float = 0.45,
                     include_implicit: bool = True) -> list[dict]:
        """Search for facts matching a query. Returns list of fact dicts with similarity scores.

        Parameters
        ----------
        query : str
            The search query string.
        limit : int, optional
            Maximum number of results to return. Defaults to 3.
        min_similarity : float, optional
            Minimum similarity threshold to include a result. Defaults to 0.45.
        include_implicit : bool, optional
            If True (default), include both explicit and implicit facts.
            If False, only return explicit facts (implicit=False).

        Returns
        -------
        list[dict]
            List of fact dictionaries with keys like 'id', 'fact', 'similarity',
            'category', 'implicit', etc.
        """
        try:
            payload = {
                "user_id": USER_ID,
                "query": query,
                "limit": limit
            }
            if not include_implicit:
                payload["implicit"] = False
            r = self.client.post(f"{self.base}/facts/search", json=payload)
            r.raise_for_status()
            facts = r.json()
            # Filter by similarity threshold
            relevant = [f for f in facts if f.get("similarity") is not None and f["similarity"] >= min_similarity]
            # Track what was actually returned so callers can update shown_fact_ids
            self.shown_fact_ids.update(
                f["id"] for f in relevant if f.get("id") is not None
            )
            return relevant
        except Exception as e:
            print(f"[memory] search_facts failed: {e}")
            return []

    def get_recent_episodes(self, query: str = "", limit: int = 2) -> list[dict]:
        """Search for recent/relevant episodes.

        Parameters
        ----------
        query : str, optional
            Search query for episode content. Empty string returns most recent episodes.
        limit : int, optional
            Maximum number of episodes to return. Defaults to 2.

        Returns
        -------
        list[dict]
            List of episode dictionaries with keys like 'speaker', 'content', etc.
        """
        try:
            r = self.client.post(f"{self.base}/episode/search", json={
                "user_id": USER_ID, "query": query, "limit": limit
            })
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[memory] get_recent_episodes failed: {e}")
            return []
