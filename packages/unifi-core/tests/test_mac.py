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
