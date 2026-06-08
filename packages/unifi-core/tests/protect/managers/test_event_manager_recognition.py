"""Tests for EventManager._license_plate_recognition_fields.

The new method mirrors the face-recognition extractor but targets LPR
events. The carrying thumbnail's ``type`` is ``"vehicle"`` (per live UNVR
data); the plate string lives at ``thumbnail.name`` and is mirrored at
``thumbnail.group.matched_name``; the plate's stable group UUID and the
recognition confidence sit on the same ``group`` sub-object.
"""

from unittest.mock import MagicMock

from unifi_core.protect.managers.event_manager import EventManager


def _make_manager() -> EventManager:
    return EventManager(MagicMock())


def _lpr_event(thumbnails: list[dict], smart_detect_types: list[str] | None = None) -> dict:
    """Build a minimal event dict resembling a uiprotect Event payload.

    The recognition extractor reads via the polymorphic ``_get`` / ``_get_any``
    helpers, so a plain dict suffices — no need for the full pydantic shape.
    """
    return {
        "id": "evt_test",
        "smart_detect_types": smart_detect_types if smart_detect_types is not None else ["licensePlate", "vehicle"],
        "metadata": {"detected_thumbnails": thumbnails},
    }


def test_plate_fields_populated_when_lpr_thumbnail_present() -> None:
    """Vehicle thumbnail with a plate string yields all three plate fields."""
    mgr = _make_manager()
    event = _lpr_event(
        thumbnails=[
            {
                "type": "vehicle",
                "name": "ABC123",
                "cropped_id": "thumb-1",
                "group": {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "name": None,
                    "matched_name": "ABC123",
                    "confidence": 86,
                },
            }
        ]
    )

    fields = mgr._license_plate_recognition_fields(event)

    assert fields["recognized_plate_text"] == "ABC123"
    assert fields["recognized_plate_group_id"] == "11111111-1111-1111-1111-111111111111"
    assert fields["recognized_plate_confidence"] == 86


def test_returns_empty_when_smart_detect_types_lacks_license_plate() -> None:
    """Vehicle-only events (no LPR) must not surface plate fields even if a thumbnail name looks plate-like."""
    mgr = _make_manager()
    event = _lpr_event(
        thumbnails=[
            {
                "type": "vehicle",
                "name": "SOMETHING",
                "group": {"id": "g1", "matched_name": "SOMETHING", "confidence": 70},
            }
        ],
        smart_detect_types=["vehicle"],  # NOT licensePlate
    )

    assert mgr._license_plate_recognition_fields(event) == {}


def test_returns_empty_when_no_thumbnail_carries_plate_text() -> None:
    """LPR was signaled but no thumbnail actually carries a plate string (rare but possible)."""
    mgr = _make_manager()
    event = _lpr_event(
        thumbnails=[
            {
                "type": "vehicle",
                "name": None,
                "group": {"id": None, "matched_name": None, "confidence": None},
            }
        ]
    )

    assert mgr._license_plate_recognition_fields(event) == {}


def test_picks_candidate_with_group_id_over_one_without() -> None:
    """When multiple plate-bearing thumbnails exist, prefer the one with a group_id."""
    mgr = _make_manager()
    event = _lpr_event(
        thumbnails=[
            {
                "type": "vehicle",
                "name": "AAA111",
                "group": {"id": None, "matched_name": "AAA111", "confidence": 99},  # higher conf but no id
            },
            {
                "type": "vehicle",
                "name": "BBB222",
                "group": {"id": "g_winner", "matched_name": "BBB222", "confidence": 70},  # lower conf but has id
            },
        ]
    )

    fields = mgr._license_plate_recognition_fields(event)

    assert fields["recognized_plate_text"] == "BBB222"
    assert fields["recognized_plate_group_id"] == "g_winner"
    assert fields["recognized_plate_confidence"] == 70


def _lpr_event_raw(thumbnails: list[dict], smart_detect_types: list[str] | None = None) -> dict:
    """Build a RAW UniFi event dict as ``api_request_list`` returns it.

    Unlike :func:`_lpr_event`, raw rows use camelCase keys (``smartDetectTypes``,
    ``detectedThumbnails``) — the shape the ``_raw_event_to_dict`` path consumes.
    The recognition extractor must read both casings or plate identity silently
    vanishes on the primary list/search/query tools.
    """
    return {
        "id": "evt_raw",
        "type": "smartDetectZone",
        "start": 1717612345000,
        "end": 1717612350000,
        "smartDetectTypes": smart_detect_types if smart_detect_types is not None else ["licensePlate", "vehicle"],
        "metadata": {
            "detectedThumbnails": thumbnails,
        },
    }


def test_plate_fields_populated_on_raw_camelcase_event() -> None:
    """The extractor must honor camelCase ``smartDetectTypes`` from raw rows."""
    mgr = _make_manager()
    event = _lpr_event_raw(
        thumbnails=[
            {
                "type": "vehicle",
                "name": "XYZ789",
                "croppedId": "thumb-raw-1",
                "group": {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "matchedName": "XYZ789",
                    "matchedGroupConfidence": 91,
                },
            }
        ]
    )

    fields = mgr._license_plate_recognition_fields(event)

    assert fields["recognized_plate_text"] == "XYZ789"
    assert fields["recognized_plate_group_id"] == "33333333-3333-3333-3333-333333333333"
    assert fields["recognized_plate_confidence"] == 91


def test_raw_event_to_dict_surfaces_plate_fields() -> None:
    """Plate identity must flow through the primary ``_raw_event_to_dict`` path.

    ``_raw_event_to_dict`` backs protect_list_events / protect_get_event /
    protect_list_smart_detections — the tools an LPR consumer actually calls.
    """
    mgr = _make_manager()
    raw = _lpr_event_raw(
        thumbnails=[
            {
                "type": "vehicle",
                "name": "PLT456",
                "group": {
                    "id": "44444444-4444-4444-4444-444444444444",
                    "matchedName": "PLT456",
                    "matchedGroupConfidence": 88,
                },
            }
        ]
    )

    result = mgr._raw_event_to_dict(raw)

    assert result["recognized_plate_text"] == "PLT456"
    assert result["recognized_plate_group_id"] == "44444444-4444-4444-4444-444444444444"
    assert result["recognized_plate_confidence"] == 88
