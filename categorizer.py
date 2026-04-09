"""Claude-powered transaction categorization.

Three-tier categorization strategy:
1. History lookup — resolve from payee cache
2. Fuzzy match — catch payee name variations
3. Claude (Haiku, batched) — genuinely novel payees only
"""
