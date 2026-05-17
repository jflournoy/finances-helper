"""Shared primitives for the review/confirm pipeline.

Holds the contract between `review.py` (which marks proposals as user-skipped)
and `amazon_confirm.py` (which honors those marks at apply time). Both modules
import from here so the sentinel string lives in exactly one place and the
dependency direction is producer → shared → consumer instead of consumer
importing from producer.
"""


REVIEW_SKIP_SENTINEL_PREFIX = "skipped-by-review"


def is_review_skip(applied_at_value) -> bool:
    """True when `applied_at` is the sentinel review.py writes for a user-skipped proposal.

    A skipped proposal carries `applied_at = "skipped-by-review:<reason>"` so that
    amazon_confirm.py's existing `applied_at` short-circuit skips it at apply time
    without needing schema changes.
    """
    return (
        isinstance(applied_at_value, str)
        and applied_at_value.startswith(REVIEW_SKIP_SENTINEL_PREFIX)
    )
