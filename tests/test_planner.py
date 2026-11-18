"""The pure rules: parsing the feed and deciding which codes belong on the lock."""
from datetime import timedelta

from custom_components.aki_bookings.planner import (
    current_booking, desired_codes, next_boundary, orphan_users, parse_bookings, record_matches_user, removal_reason, upcoming_booking,
)

from .conftest import NOW, booking


def parse(*raws):
    bookings, bad = parse_bookings(list(raws))
    assert not bad
    return bookings


def test_pin_keeps_leading_zeros_and_is_not_printed():
    (b,) = parse(booking(pin="003957"))
    assert b.pin == "003957"
    assert "003957" not in repr(b)


def test_numeric_pin_is_rejected():
    bookings, bad = parse_bookings([booking(pin=3957)])
    assert not bookings and len(bad) == 1 and "3957" not in bad[0]


def test_cancelled_has_no_code_and_null_names_are_fine():
    (b,) = parse(booking(status="cancelled", guest_first_name=None, guest_locale=None))
    assert b.code_id is None and b.guest_locale == "pl"
    assert desired_codes([b], NOW) == {}


def test_window_edges():
    (b,) = parse(booking())  # 08:00-10:00, code 07:45-10:10
    assert not desired_codes([b], NOW - timedelta(minutes=15, seconds=1))
    assert desired_codes([b], NOW - timedelta(minutes=15))
    assert desired_codes([b], NOW + timedelta(hours=2, minutes=9, seconds=59))
    assert not desired_codes([b], NOW + timedelta(hours=2, minutes=10))


def test_revoked_code_is_not_wanted():
    (b,) = parse(booking())
    assert desired_codes([b], NOW, {"code-1"}) == {}


def test_adjacent_bookings_overlap_in_code_windows():
    a, b = parse(booking("AB12CD", NOW, 2, "111111", "c1"), booking("ZZ99ZZ", NOW + timedelta(hours=2), 1, "222222", "c2"))
    assert set(desired_codes([a, b], NOW + timedelta(hours=1, minutes=50))) == {"c1", "c2"}


def test_next_boundary_and_current_next():
    a, b = parse(booking("AB12CD", NOW, 2, "111111", "c1"), booking("ZZ99ZZ", NOW + timedelta(hours=5), 1, "222222", "c2"))
    assert next_boundary([a, b], NOW) == NOW + timedelta(hours=2)
    assert next_boundary([a, b], NOW + timedelta(hours=2)) == NOW + timedelta(hours=2, minutes=10)
    assert current_booking([a, b], NOW).ref == "AB12CD"
    assert upcoming_booking([a, b], NOW).ref == "ZZ99ZZ"
    assert current_booking([a, b], NOW + timedelta(hours=3)) is None


def test_removal_reasons():
    rec = {"booking_id": "id-AB12CD", "ref": "AB12CD"}
    assert removal_reason("code-1", rec, parse(booking(status="cancelled")), NOW, set()) == "cancelled"
    assert removal_reason("code-1", rec, parse(booking(code="code-2")), NOW, set()) == "regenerated"
    assert removal_reason("code-1", rec, parse(booking()), NOW + timedelta(hours=3), set()) == "ended"
    assert removal_reason("code-1", rec, [], NOW, set()) == "ended"
    assert removal_reason("code-1", rec, parse(booking()), NOW, {"code-1"}) == "revoked"


def test_slot_reused_by_hand_is_never_ours():
    named = {"ref": "AB12CD", "named": True, "user_index": 3}
    unnamed = {"ref": "AB12CD", "named": False, "user_index": 3}
    assert record_matches_user(named, {"user_index": 3, "user_name": "R-AB12CD"})
    assert not record_matches_user(named, {"user_index": 3, "user_name": "Kasia"})
    assert not record_matches_user(named, {"user_index": 3, "user_name": "R-OTHER1"})
    assert record_matches_user(unnamed, {"user_index": 3, "user_name": None})
    assert not record_matches_user(unnamed, {"user_index": 3, "user_name": "Kasia"})
    assert not record_matches_user(named, None)


def test_orphans_are_only_unaccounted_res_users():
    users = [{"user_index": 1, "user_name": "Kasia"}, {"user_index": 2, "user_name": "R-AB12CD"}, {"user_index": 3, "user_name": "RES-OLD001"}, {"user_index": 4, "user_name": None}]
    assert [u["user_index"] for u in orphan_users(users, {"c": {"user_index": 2}})] == [3]
