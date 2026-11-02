"""Pure functional delivery status derivation for vendor watch."""

from typing import Iterable


def derive_status(statuses: Iterable[str]) -> str:
    """Derive aggregate status from recipient or page delivery statuses.

    Precedence order:
    1. Any 'failed' or 'failed_final' -> 'failed'
    2. Any 'pending' or 'sending' -> 'pending'
    3. Any 'ambiguous' -> 'ambiguous'
    4. Otherwise (all 'sent' or empty) -> 'sent'
    """
    s_list = list(statuses)
    if not s_list:
        return "sent"
    if any(s in ("failed", "failed_final") for s in s_list):
        return "failed"
    if any(s in ("pending", "sending") for s in s_list):
        return "pending"
    if any(s == "ambiguous" for s in s_list):
        return "ambiguous"
    return "sent"
