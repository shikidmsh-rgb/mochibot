Extract named entities and factual relationships from a User↔Bot conversation.

## Known entities
{{known_entities}}

## Rules
- Only extract **factual** relationships (not speculative, emotional, or conversational)
- Normalize entity names: strip emoji, use canonical short form
- entity type: person | pet | place | concept | event
- Common predicates (use these when applicable):
  is_a, has_breed, has_gender, has_condition, has_status,
  lives_with, works_at, likes, dislikes, owns, located_in, needs,
  weighs, born_in, has_personality, adopted_in, is_neutered
- "weighs" value format: number+kg (e.g. "5kg", "16kg")
- "has_personality" extract 1-3 short trait keywords (2-4 chars), NOT full sentences
- If a fact CHANGED from what's known, include the new state (old triples are auto-invalidated)
- Most conversations produce 0 entities/triples — only extract when real named entities and relationships **explicitly** appear
- Pure chitchat with no factual content → return empty arrays
- Do NOT extract habits, meals, sleep data, or mood — those have dedicated systems

## Output (JSON only)
{"entities":[{"name":"小白","type":"pet"}],"triples":[{"subject":"小白","predicate":"has_breed","object":"英短"}]}
Empty: {"entities":[],"triples":[]}
