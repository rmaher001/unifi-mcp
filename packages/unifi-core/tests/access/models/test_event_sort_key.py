"""Access events need one canonical ordering identity, shared by every surface.

Both API surfaces sorted on `(raw["timestamp"], raw["id"])`. System-log rows
carry neither: the time is `published` (epoch ms) and the id is the empty
string. Every row therefore keyed to `(0, "")`, and since `paginate()` windows
with `(ts, id) < (last_ts, last_id)`, page 2 of an events query came back
empty - nothing can be strictly less than the key every row shares.
"""

from unifi_core.access.models.events import event_sort_key

SYSTEM_LOG_ROW = {
    "id": "",
    "log_key": "access.door.unlock",
    "event_type": "access.door.unlock",
    "message": "Access Granted (Face)",
    "published": 1787054400000,
    "result": "ACCESS",
}


def test_a_system_log_row_has_a_real_timestamp_not_zero() -> None:
    assert event_sort_key(SYSTEM_LOG_ROW)[0] > 0


def test_rows_order_by_published_time() -> None:
    older = {**SYSTEM_LOG_ROW, "published": 1787054391000}
    newer = {**SYSTEM_LOG_ROW, "published": 1787054490000}
    assert event_sort_key(older) < event_sort_key(newer)


def test_distinct_rows_never_share_a_key() -> None:
    """The cursor filter is strict `<`, so any two rows sharing a key make the
    next page drop both."""
    a = {**SYSTEM_LOG_ROW, "published": 1787054400000, "message": "Access Granted (Face)"}
    b = {**SYSTEM_LOG_ROW, "published": 1787054400000, "message": "Door status - Opened"}
    assert event_sort_key(a) != event_sort_key(b)


def test_sub_second_ordering_is_preserved() -> None:
    """`published` is milliseconds; truncating to whole seconds collapses
    events that happen within the same second into one key."""
    a = {**SYSTEM_LOG_ROW, "published": 1787054400100}
    b = {**SYSTEM_LOG_ROW, "published": 1787054400900}
    assert event_sort_key(a) < event_sort_key(b)


def test_a_real_id_is_used_as_the_identity_when_present() -> None:
    row = {**SYSTEM_LOG_ROW, "id": "evt-1"}
    assert event_sort_key(row)[1] == "evt-1"


def test_the_key_is_reproducible_across_calls() -> None:
    """Cursors are handed back to a later request; an unstable identity would
    silently skip or repeat rows."""
    assert event_sort_key(SYSTEM_LOG_ROW) == event_sort_key(dict(SYSTEM_LOG_ROW))


def test_legacy_websocket_rows_still_key() -> None:
    legacy = {"id": "evt-1", "type": "access.door.unlock", "timestamp": "2026-08-18T12:00:00Z"}
    ts, ident = event_sort_key(legacy)
    assert ts > 0
    assert ident == "evt-1"


def test_a_row_with_no_time_at_all_sorts_last_rather_than_raising() -> None:
    ts, ident = event_sort_key({"id": "evt-2"})
    assert ts == 0
    assert ident == "evt-2"


def test_the_key_is_comparable_across_a_mixed_list() -> None:
    """sorted() raises TypeError the moment a str meets an int in the tuple."""
    rows = [SYSTEM_LOG_ROW, {"id": "evt-2"}, {"id": "evt-3", "timestamp": "2026-08-18T12:00:00Z"}]
    assert len(sorted(rows, key=event_sort_key)) == 3
