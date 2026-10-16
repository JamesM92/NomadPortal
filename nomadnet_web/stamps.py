"""Stamp cost values for incoming-message proof-of-work.

LXMF lets an identity ask senders to attach a "stamp": proof of work
whose difficulty is the cost. Each +1 doubles the work, so the cap below
is a limit on what an ordinary phone can be asked to do: 24 is about
sixteen million hashes, noticeable but finishable, and the higher
reaches of LXMF's own range are not realistic for anyone sending a chat
message from a small device.
"""

from typing import Optional

MAX_STAMP_COST = 24


def parse_stamp_cost(value) -> Optional[int]:
    """A cost from a setting or form value, clamped to 0..MAX_STAMP_COST
    (0 means "no stamp required"). None when the value is not a number."""
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, min(MAX_STAMP_COST, n))
