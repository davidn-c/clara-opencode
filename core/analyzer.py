"""
core/analyzer.py — Importance scoring and fact extraction from conversation exchanges.

Extracted from clara2.py to decouple analysis logic from the monolithic
script. Wraps llama_analyze() with the analysis prompt and parses the
JSON response into facts stored via the memory client.

Original function signature: analyze_exchange(memory, user_input, clara_reply, skip_facts=False)
"""

import re
import json
import time

from core.services import PIN_PHRASES
from core.llm_client import llama_analyze

VALID_CATEGORIES = [
    "preference", "routine", "person", "device", "media",
    "health", "work", "inferred", "household"
]


def analyze_exchange(
    memory: object,
    user_input: str,
    clara_reply: str,
    skip_facts: bool = False,
) -> None:
    """Analyze a conversation exchange for importance scoring and fact extraction.

    This is the direct port of the original ``analyze_exchange`` from
    clara2.py.  It writes episodes and facts into *memory* on success.

    Parameters
    ----------
    memory : object
        Memory client with ``write_episode()`` and ``write_fact()`` methods.
    user_input : str
        The raw text the user spoke / typed.
    clara_reply : str
        Clara's response to the user input (used for context in fact extraction).
    skip_facts : bool, optional
        When ``True`` only logs the episode and skips fact extraction
        (used when a CORRECT: hop already handled this turn).
    """
    time.sleep(3)
    pin_requested = any(p in user_input.lower() for p in PIN_PHRASES)

    if skip_facts:
        print(f"[analyze] correction already applied this turn — logging episode, skipping fact extraction")
        memory.write_episode("user", user_input, 0.8)
        return

    if len(user_input.split()) < 8:
        print(f"[analyze] short message — importance: 0.2, skipping fact extraction")
        memory.write_episode("user", user_input, 0.2)
        return

    combined_prompt = f"""
Analyze this conversation exchange and return a JSON object with exactly three fields:
- "importance": a float between 0.0 and 1.0 scoring how important this is for long-term memory
- "facts": an array of facts worth storing
- "mentions": an array of person names mentioned in Dave's message (only real people, not Clara)

Importance scoring — use the FULL range 0.0 to 1.0, do not default to 0.5:
High (0.7-1.0): personal facts about Dave or Susan, facts about their environment, their values, their goals, teaching moments directed at Dave/Susan, strong emotion expressed BY Dave or Susan
Medium (0.4-0.6): useful context, preferences, questions revealing interests
Low (0.0-0.3): casual chitchat, transactional exchanges, philosophical or speculative discussion about AI/consciousness/reasoning that is not a factual claim about Dave or Susan

Most meaningful conversations about Susan, Dave, cats, gecko, life, work, or family should score 0.7 or above.

Fact extraction rules:
- ONLY extract clear, specific facts ABOUT Dave or Susan themselves: their lives, preferences, family, routines, goals, or stated opinions.
- STRICT EXCLUSION: never extract facts, claims, or speculation about Clara's own nature, experience, consciousness, reasoning process, or capabilities — even if Dave or Clara raised the topic. A conversation ABOUT AI consciousness is not a fact ABOUT Dave, unless it is narrowly "Dave's stated opinion on X" (e.g. "Dave is skeptical that AI has subjective experience") and even then only store it as Dave's opinion, never as a standalone claim about AI.
- Do not extract general knowledge, hypotheticals, or abstract ideas — only concrete facts about Dave/Susan's actual lives.
- Valid categories (ONLY these 9 exact values, no others): preference, routine, person, device, media, health, work, inferred, household
- NEVER use "family", "pet", or any other category not in the list above — use "person" or "household" instead
- If unsure whether something is a fact worth storing, do NOT store it — leave the facts array empty rather than guessing
- If no facts found, return empty array

Mentions tracking:
- Extract ALL person names mentioned by Dave in his message (not Clara's reply)
- Include names of family members, friends, colleagues, acquaintances — anyone Dave refers to by name
- Do NOT include Clara, Dave, or Susan (those are tracked separately)
- Return as simple string array: ["John", "Mary"]
- If no other people mentioned, return empty array []

Exchange:
Dave: {user_input}
Clara: {clara_reply}

Return ONLY valid JSON, no explanation, no markdown:
{{"importance": 0.8, "facts": [{{"category": "person", "fact": "example", "confidence": 0.9}}], "mentions": ["John", "Mary"]}}"""

    importance = 0.5
    try:
        raw = llama_analyze(combined_prompt)
        raw = re.sub(r'```json|```', '', raw).strip()
        # Strip thinking block if Gemma emits one
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        start = raw.find('{')
        if start == -1:
            raise ValueError(f"No JSON object found in response: {raw[:100]}")
        depth = 0
        end = -1
        for i in range(start, len(raw)):
            if raw[i] == '{':
                depth += 1
            elif raw[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            raise ValueError(f"Unbalanced JSON object in response: {raw[:100]}")
        json_str = raw[start:end + 1]
        result = json.loads(json_str)
        raw_importance = result.get("importance", 0.5)
        importance = float(raw_importance) if raw_importance is not None else 0.5
        importance = max(0.0, min(1.0, importance))
        print(f"[analyze] importance: {importance} for: {user_input[:50]}")
        memory.write_episode("user", user_input, importance)
        
        # Track mentions of other people — only if the name actually appears
        # in Dave's message (not just in the LLM's fact extraction)
        mentions = result.get("mentions", [])
        user_lower = user_input.lower()
        if mentions and isinstance(mentions, list):
            for name in mentions:
                if isinstance(name, str) and name.strip():
                    name = name.strip()
                    # Only track if name actually appears in Dave's input
                    if name.lower() not in user_lower:
                        print(f"[analyze] skipping mention '{name}' — not in user input")
                        continue
                    try:
                        memory.track_mention(name)
                        print(f"[analyze] mention tracked: {name}")
                    except Exception as e:
                        print(f"[analyze] mention tracking failed for {name}: {e}")
        
        facts = result.get("facts", [])
        if facts:
            for f in facts:
                if isinstance(f, str):
                    f = {"category": "inferred", "fact": f, "confidence": 0.6}
                if not isinstance(f, dict):
                    continue
                category = f.get("category", "inferred")
                if category not in VALID_CATEGORIES:
                    category = "inferred"
                fact = f.get("fact", "")
                confidence = float(f.get("confidence") or 0.8)
                if fact:
                    # Smart implicit/explicit detection:
                    # - Facts in the "inferred" category are implicit by definition
                    # - Low-confidence facts (< 0.6) are treated as implicit/speculative
                    # - All other facts are explicit (directly stated/confirmed)
                    is_implicit = (category == "inferred") or (confidence < 0.6)
                    ok = memory.write_fact(category, fact, confidence, implicit=is_implicit, pinned=pin_requested)
                    if ok:
                        tag = "PINNED fact" if pin_requested else ("INFERRED fact" if is_implicit else "fact stored")
                        print(f"[analyze] {tag}: [{category}] {fact[:60]}")
                    else:
                        print(f"[analyze] FACT WRITE FAILED (not stored): [{category}] {fact[:60]}")
        else:
            print(f"[analyze] no new facts found")
    except Exception as e:
        print(f"[analyze] analysis failed: {e}")
        memory.write_episode("user", user_input, importance)