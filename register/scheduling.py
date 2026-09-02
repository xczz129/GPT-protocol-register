"""Registration task wave planning."""

from __future__ import annotations

from mail.parsing import base_mailbox_address


def _is_alias(email: str) -> bool:
    address = str(email or "").strip().lower()
    return "@" in address and "+" in address.rsplit("@", 1)[0]


def build_registration_waves(
    emails: list[str],
    concurrency: int,
    *,
    fixed_alias_batch: bool = False,
) -> list[tuple[list[int], bool]]:
    """Return 1-based task-index waves and whether each wave is an alias pair.

    Complete adjacent alias pairs run as isolated waves. The next pair is not
    dispatched until both tasks in the current wave have fully returned.
    Non-paired tasks retain ordinary concurrency-based batching.
    """
    task_count = len(emails)
    if task_count <= 0:
        return []
    if fixed_alias_batch:
        return [(list(range(1, task_count + 1)), task_count == 2)]

    width = max(1, int(concurrency or 1))
    waves: list[tuple[list[int], bool]] = []
    regular: list[int] = []

    def flush_regular() -> None:
        if regular:
            # The scheduler's semaphore keeps this wave at ``width`` active
            # tasks and immediately backfills a freed slot, matching the
            # original behavior for non-paired registrations.
            waves.append((list(regular), False))
            regular.clear()

    index = 0
    while index < task_count:
        email = emails[index]
        paired = bool(
            index + 1 < task_count
            and _is_alias(email)
            and _is_alias(emails[index + 1])
            and base_mailbox_address(email)
            == base_mailbox_address(emails[index + 1])
        )
        if paired:
            flush_regular()
            waves.append(([index + 1, index + 2], True))
            index += 2
            continue
        regular.append(index + 1)
        index += 1
    flush_regular()
    return waves
