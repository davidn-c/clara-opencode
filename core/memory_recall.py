"""
core/memory_recall.py — recall_memory(): fetch relevant facts/episodes for context.

Extracted from clara2.py to decouple memory recall from the monolithic
script. Queries the memory client for facts and recent episodes relevant
to the current conversation turn.

Original function signature: recall_memory(memory, query, facts_only=False)
"""

from core.memory_client import MemoryClient


def recall_memory(memory: MemoryClient, query: str, facts_only: bool = False) -> str:
    """Fetch relevant facts (and optionally recent episodes) for the given query.

    This is the direct port of the original ``recall_memory`` from clara2.py.
    It operates on an *already-initialised* MemoryClient instance.

    Parameters
    ----------
    memory : MemoryClient
        Active memory client (must have a live session).
    query : str
        The user input or tool-token query to search for.
    facts_only : bool, optional
        When ``True`` returns only the formatted facts string.
        When ``False`` (default) returns facts + recent episodes.

    Returns
    -------
    str
        Formatted text suitable for embedding in an LLM prompt.
    """
    facts_text = ""
    episodes_text = ""

    # 1. Retrieve matching facts from the memory client
    matching_facts = memory.search_facts(query)
    if matching_facts:
        for i, f in enumerate(matching_facts, 1):
            pin_tag = " [PINNED]" if f.get("pinned") else ""
            implicit_tag = " [INFERRED]" if f.get("implicit") else ""
            facts_text += f"{i}. [{f.get('category', 'inferred')}] {f['fact']}{implicit_tag}{pin_tag} (confidence: {f.get('confidence', 0):.2f})\n"

    # 2. Retrieve recent episodes for richer context
    episodes_text = ""
    if not facts_only:
        recent = memory.get_recent_episodes(limit=5)
        if recent:
            for i, e in enumerate(recent, 1):
                episodes_text += f"{i}. {e.get('speaker', 'unknown')}: {e.get('content', '')}\n"

    # 3. Combine and return
    result = ""
    if facts_text:
        result += "Relevant memories:\n" + facts_text
    if episodes_text:
        result += "\nRecent conversation:\n" + episodes_text

    if not result:
        result = "No matching memories found."

    return result