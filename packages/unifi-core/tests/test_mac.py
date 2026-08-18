"""Tests for MAC address normalization.

The UniFi controller reports MAC addresses in lowercase. Callers - humans
typing into an agent, values copy-pasted out of a vendor label or another
tool's output - routinely supply uppercase. Comparing the two forms with a
raw `==` silently reports "not found" for a device that plainly exists.
"""

from unifi_core.mac import mac_equal, normalize_mac


def test_uppercase_is_lowercased() -> None:
    assert normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"


def test_mixed_case_is_lowercased() -> None:
    assert normalize_mac("Aa:bB:Cc:dD:Ee:fF") == "aa:bb:cc:dd:ee:ff"


def test_lowercase_is_unchanged() -> None:
    assert normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"


def test_surrounding_whitespace_is_stripped() -> None:
    assert normalize_mac("  AA:BB:CC:DD:EE:FF\n") == "aa:bb:cc:dd:ee:ff"


def test_separators_are_left_alone() -> None:
    """Deliberately NOT reformatted. Case is the defect being fixed; silently
    rewriting separators would change which strings match on a guess about
    what the caller meant."""
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa-bb-cc-dd-ee-ff"


def test_empty_and_blank_become_none() -> None:
    assert normalize_mac("") is None
    assert normalize_mac("   ") is None


def test_non_string_becomes_none() -> None:
    assert normalize_mac(None) is None
    assert normalize_mac(1234) is None


def test_mac_equal_matches_across_case() -> None:
    assert mac_equal("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff")
    assert mac_equal("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF")


def test_mac_equal_rejects_different_addresses() -> None:
    assert not mac_equal("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:00")


def test_mac_equal_is_false_when_either_side_is_missing() -> None:
    """A record with no `mac` field must never match, and an empty query must
    never match a record with no `mac` field. Both normalize to None, and
    None == None would otherwise be a spurious hit."""
    assert not mac_equal(None, None)
    assert not mac_equal("", None)
    assert not mac_equal("aa:bb:cc:dd:ee:ff", None)
    assert not mac_equal(None, "aa:bb:cc:dd:ee:ff")


# --- Access device manager --------------------------------------------------


# The Access device manager's own behavioural coverage lives in
# tests/access/managers/test_device_manager_mac.py. It matches on `mac` case
# -insensitively while keeping `unique_id` an exact comparison - not because
# unique_ids are non-hex (several Access id classes are hex; see
# access/models/device_configs.py), but because they are opaque controller
# identifiers rather than addresses, so there is no case-equivalence rule to
# apply to them.


# --- Model-boundary normalization -------------------------------------------


def test_acl_mac_side_lowercases_the_caller_supplied_addresses() -> None:
    """The controller stores these lowercase, so a create-then-list round trip
    is case-asymmetric unless the create side normalizes."""
    from unifi_core.network.models.acl import _create_side

    side = _create_side(["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"], None, None)
    assert side["specific_mac_addresses"] == ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]


def test_acl_mac_side_leaves_unusable_entries_alone() -> None:
    """Normalization must not silently drop or rewrite something it cannot
    parse - that would hide a bad input rather than surface it."""
    from unifi_core.network.models.acl import _create_side

    assert _create_side([""], None, None)["specific_mac_addresses"] == [""]


def test_traffic_flow_query_lowercases_the_source_mac_filter() -> None:
    """A server-side filter that does not match returns 200 with zero flows -
    the quiet failure mode."""
    from unifi_core.network.models.traffic_flows import TrafficFlowQuery

    q = TrafficFlowQuery(source_mac=["AA:BB:CC:DD:EE:FF"])
    assert q.source_mac == ["aa:bb:cc:dd:ee:ff"]


# --- looks_like_mac ---------------------------------------------------------


def test_looks_like_mac_accepts_the_forms_a_controller_or_a_label_uses() -> None:
    from unifi_core.mac import looks_like_mac

    assert looks_like_mac("aa:bb:cc:dd:ee:ff")
    assert looks_like_mac("AA:BB:CC:DD:EE:FF")
    assert looks_like_mac("aa-bb-cc-dd-ee-ff")
    assert looks_like_mac("aabbccddeeff")


def test_looks_like_mac_rejects_opaque_identifiers() -> None:
    """The case this exists for: an Access unique_id contains a separator
    (`dev-1`) or is hex of the wrong length, and must not be mistaken for an
    address and sent off for resolution."""
    from unifi_core.mac import looks_like_mac

    assert not looks_like_mac("dev-1")
    assert not looks_like_mac("0123456789abcdef01234567")  # 24-hex Access unique_id
    assert not looks_like_mac("aa:bb:cc:dd:ee")  # too short
    assert not looks_like_mac("aa:bb-cc:dd:ee:ff")  # mixed separators
    assert not looks_like_mac(None)
    assert not looks_like_mac("")


def test_oon_targets_of_type_mac_are_lowercased() -> None:
    """`normalize_oon_create_payload` names itself for this and did not do it.

    The parameter is called `targets`, not `*_mac`, so a sweep for MAC-typed
    parameters cannot find it - but the tool layer documents it as a list of
    target MAC addresses.
    """
    from unifi_core.network.managers.oon_manager import _normalize_oon_targets

    assert _normalize_oon_targets("CLIENTS", ["AA:BB:CC:DD:EE:FF"]) == [{"type": "MAC", "value": "aa:bb:cc:dd:ee:ff"}]
    assert _normalize_oon_targets("CLIENTS", [{"mac": "AA:BB:CC:DD:EE:FF"}]) == [
        {"type": "MAC", "value": "aa:bb:cc:dd:ee:ff"}
    ]


def test_oon_targets_of_other_types_are_left_alone() -> None:
    """A NETWORK_GROUP_ID is not an address and must not be case-folded."""
    from unifi_core.network.managers.oon_manager import _normalize_oon_targets

    assert _normalize_oon_targets("GROUPS", ["GroupID-ABC"]) == [{"type": "NETWORK_GROUP_ID", "value": "GroupID-ABC"}]


def test_client_wifi_details_cache_key_is_case_stable() -> None:
    """A process-lifetime cache keyed on the raw MAC fetches /stat/sta twice
    for the same client."""
    import inspect

    from unifi_core.network.managers.stats_manager import StatsManager

    src = inspect.getsource(StatsManager.get_client_wifi_details)
    normalize_at = src.index("normalize_mac(client_mac)")
    cache_at = src.index("cache_key =")
    assert normalize_at < cache_at, "the cache key is built before the MAC is normalized"
