"""MAC address normalization.

The UniFi controller reports MAC addresses in lowercase. Callers supply
whatever they have - and the form printed on a device label, quoted in
vendor documentation, or pasted out of another tool is very often
uppercase. Comparing the two with a raw ``==`` reports "not found" for
hardware that plainly exists, which reads as a broken controller rather
than a case mismatch.

Use :func:`mac_equal` for comparisons rather than normalizing both sides at
each call site: it is the guard against ``None == None`` matching a record
that has no ``mac`` field at all.
"""

from typing import Any, Optional

__all__ = ["normalize_mac", "mac_equal"]


def normalize_mac(mac: Any) -> Optional[str]:
    """Return *mac* lowercased and stripped, or ``None`` if it is not usable.

    Separators are deliberately left alone. Case is the defect this exists to
    fix; rewriting ``-`` to ``:`` would change which strings match based on a
    guess about what the caller meant, and the controller's own payloads are
    consistently colon-separated anyway.

    Anything that is not a non-empty string - ``None``, a missing dict key, an
    int - normalizes to ``None`` so it can never compare equal to a real
    address.
    """
    if not isinstance(mac, str):
        return None
    return mac.strip().lower() or None


def mac_equal(a: Any, b: Any) -> bool:
    """Return True if *a* and *b* are the same MAC address, ignoring case.

    Returns False whenever either side is missing or unusable. That guard is
    load-bearing: both would normalize to ``None``, and a bare ``==`` would
    then report a match between an empty query and a record carrying no
    ``mac`` field.
    """
    normalized = normalize_mac(a)
    return normalized is not None and normalized == normalize_mac(b)
