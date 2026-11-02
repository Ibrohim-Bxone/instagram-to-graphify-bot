"""Pure functional delivery status derivation for vendor watch."""

from typing import Any, Iterable


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


def _extract_status(item: Any) -> str:
    """Extract status string from delivery row (tuple, Row, dict, or str)."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("status", ""))
    if hasattr(item, "keys"):
        if "status" in item.keys():
            return str(item["status"])
        return str(item[1] if len(item) > 1 else item[0])
    if isinstance(item, (tuple, list)):
        return str(item[1] if len(item) > 1 else item[0])
    return str(item)


def _extract_chat_id(item: Any) -> str | None:
    """Extract chat_id from delivery row if present."""
    if isinstance(item, dict):
        if "chat_id" in item:
            return str(item["chat_id"])
        return None
    if hasattr(item, "keys"):
        if "chat_id" in item.keys():
            return str(item["chat_id"])
        if len(item) > 1:
            return str(item[0])
        return None
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        return str(item[0])
    return None


def derive_recipient_status(deliveries: Iterable[Any]) -> str:
    """Derive effective status for a single recipient-page pair from its delivery rows (D1).

    Rows are expected in rowid order (the order returned by the DB query).
    Precedence order:
    1. If any row is 'sent' -> 'sent'
    2. Else if any row is 'ambiguous' -> 'ambiguous'
    3. Else status of the latest row (last row in rowid / list order)
    """
    deliv_list = list(deliveries)
    if not deliv_list:
        return "sent"

    statuses = [_extract_status(item) for item in deliv_list]

    # 1. Any 'sent' -> 'sent'
    if any(st == "sent" for st in statuses):
        return "sent"

    # 2. Else any 'ambiguous' -> 'ambiguous'
    if any(st == "ambiguous" for st in statuses):
        return "ambiguous"

    # 3. Else status of the latest row (by rowid order)
    return statuses[-1]


def derive_deliveries_status(deliveries: Iterable[Any]) -> str:
    """Group delivery records by chat_id, apply derive_recipient_status, and derive aggregate status (D2).

    Fulfills D2 requirement to group by recipient before deriving status across all callers.
    """
    deliv_list = list(deliveries)
    if not deliv_list:
        return derive_status([])

    groups: dict[str, list[Any]] = {}
    has_chat_id = False

    for item in deliv_list:
        cid = _extract_chat_id(item)
        if cid is not None:
            has_chat_id = True
            groups.setdefault(cid, []).append(item)

    if not has_chat_id:
        return derive_status([_extract_status(item) for item in deliv_list])

    effective_statuses = [
        derive_recipient_status(group_rows)
        for group_rows in groups.values()
    ]
    return derive_status(effective_statuses)

