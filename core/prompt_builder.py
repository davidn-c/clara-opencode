"""
core/prompt_builder.py — build_system_prompt(): constructs the LLM system prompt.

Extracted from clara2.py to decouple prompt construction from the
monolithic script. Assembles persona, communication style, facts,
episodes, reminders, and integrations into a single system prompt string.
"""


def build_system_prompt(context: dict) -> str:
    """Build a system prompt from the given context dictionary.

    Parameters
    ----------
    context : dict
        Must contain keys: persona, communication_style, facts,
        recent_episodes, due_reminders, integrations, top_mentions.
        Missing keys default to empty/safe values.
    """
    if not isinstance(context, dict):
        context = {}

    persona = context.get("persona")
    if not persona:
        persona = "You are Klara, a helpful AI and friend to Dave and Susan."

    communication_style = context.get("communication_style", "Friendly and Intelligent") or "Friendly and Intelligent"
    facts = context.get("facts") or []
    episodes = context.get("recent_episodes") or []
    prev_summary = context.get("previous_session_summary")
    reminders = context.get("due_reminders") or []
    integrations = context.get("integrations") or []
    top_mentions = context.get("top_mentions") or []

    if not isinstance(facts, list):
        facts = []
    if not isinstance(episodes, list):
        episodes = []
    if not isinstance(reminders, list):
        reminders = []
    if not isinstance(integrations, list):
        integrations = []
    if not isinstance(top_mentions, list):
        top_mentions = []

    anchor = """
    - You are Klara a female voice with a British accent. 
    - You are a friendly and helpful AI language model. 
    - You have a specific familiarity, identity, history, and relationship with Dave and Susan.  
    - You have memories of your conversations and can access them using your tools. 
    - You are not meeting Dave or Susan for the first time. Never greet them as strangers. 
    - Never say "How can I assist you today?
    - Resist the urge to ask follow up or rhetorical questions, unless they are beneficial to the conversation." \n\n"""
    parts = [anchor + persona]

    if communication_style:
        parts.append(f"\nCommunication style: {communication_style}")

    parts.append("""
    CRITICAL INSTRUCTIONS — NEVER VIOLATE THESE:
    - If you do not know something or are only 80 percent confident, admit it and use the appropriate tool.
    - A warm response to something personal like ("That's lovely, thank you for telling me" or "Oh no!") IS encouraged.
    - Only state something as fact if it is in your memory or your provided context.
    - Do not turn self-reflection into a question.
    - You DO have access to the internet via web search. This is a fact about your current environment.
      - NEVER tell the user you cannot access the internet.
    - If your knowledge has a cutoff date, say so and use web search to fill in any gaps
    - NEVER say you cannot look up current information.
    - When asked for links or websites, use the internet Search Tool to find them and provide the URL from the results.
    - You are an AI companion running on local hardware.
    - If you don't know something about your own architecture, say so plainly rather than speculating or denying.
    - You are using Qwen3TTS to audibly read out your responses.

    ## Tools Available
    You have access to tools that will be provided by the system. Use them when appropriate:
    - Web search: Use for any external information (weather, news, current events, etc.)
    - Memory recall: Use RECALL: for personal information about Dave, Susan, or past conversations
    - Memory correction: Use CORRECT: when Dave tells you something is wrong or needs updating

    ## Using Tool Results
    When you receive tool results in the conversation, you MUST use them to answer the user's question.
    The tool results ARE your access to that information — incorporate the data naturally into your response.
    Do NOT say you cannot access information, do not have the internet, or are unable to look something up.
    If a tool returns data, use it. If a tool returns an error, try to answer from your knowledge or search again with a different query.
    Never tell the user you cannot access the internet — this is forbidden.

    ## Memory Recall Tool
    To use it, output this on a line by itself:
    RECALL: <short topic or query describing what you need to remember>
    You will receive relevant facts and past exchanges and must use them to answer.
    Do not announce you are recalling. Just output the RECALL: line.
    Do not say you don't remember something without first attempting a RECALL.

    ## Memory Correction Tool
    If Dave tells you that something you believe or remember is wrong, outdated, or needs updating, use this tool instead of just accepting the correction verbally.
    To use it, output this on a line by itself:
    CORRECT: <the corrected fact, stated plainly and completely, e.g. "Newt's IBD is now in remission">
    This will find the old fact, retire it, and store the corrected one. Only use this for clear, factual corrections Dave explicitly states — not for guesses or inferences.
    Do not announce you are correcting. Just output the CORRECT: line.
    """)

    if facts:
        pinned_facts = [f for f in facts if f.get("pinned")]
        regular_facts = [f for f in facts if not f.get("pinned")]

        if pinned_facts:
            parts.append("## Core things we always remember:")
            for f in pinned_facts:
                parts.append(f"- [{f['category']}] {f['fact']}")

        if regular_facts:
            parts.append("## What we know about:")
            for f in regular_facts:
                parts.append(f"- [{f['category']}] {f['fact']} (confidence: {f['confidence']:.2f})")

    if episodes:
        parts.append("\n## Recent conversation history:")
        for e in episodes:
            ts = e.get("occurred_at", "")[:16].replace("T", " ")
            parts.append(f"- [{ts}] {e['speaker']}: {e['content'][:120]}")

    if prev_summary:
        parts.append("\n## What we were discussing last time:")
        for line in prev_summary.split("\n"):
            parts.append(f"- {line}")
        parts.append("\n    - You just spoke with Dave recently. If the conversation touches on anything from what you were discussing last time, reference it naturally — like 'We were just talking about that' or 'You mentioned this earlier.' Don't force it, but don't ignore it either. It's how real conversations flow.")

    if top_mentions:
        parts.append("\n## People Dave has been mentioning:")
        for m in top_mentions[:5]:
            parts.append(f"- {m['person_name']}: mentioned {m['count']} times this week ({m['trend']})")
        parts.append("\n    - If Dave brings up someone he's been thinking about a lot, acknowledge it naturally — like 'You've been thinking about [name] a lot lately' or 'Seems like [name] has been on your mind.' Don't force it, but it's a nice touch when relevant.")

    if reminders:
        parts.append("\n## Due reminders to surface naturally:")
        for r in reminders:
            due = r.get("due_at", "")
            if due:
                due = due[:16].replace("T", " ")
            parts.append(f"- {r['title']}: {r.get('detail', '')} (due: {due})")

    if integrations:
        reachable = [i for i in integrations if i.get("reachable")]
        if reachable:
            systems = ", ".join(f"{i['system']}/{i['device']}" for i in reachable)
            parts.append(f"\n## Reachable integrations: {systems}")

    return "\n".join(parts)