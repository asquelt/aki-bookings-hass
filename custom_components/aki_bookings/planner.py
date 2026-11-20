"""Pure decision logic: booking records, which codes belong on the lock, what to change.

No Home Assistant imports here, so the rules can be tested with plain pytest.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Matter allows 10 characters for a user name, but the Aqara U200 keeps only 9 ("RES-FZKBEP" came
# back as "RES-FZKBE\x00"), so the name is "R-" plus the 6-character ref: 8 characters.
USER_PREFIX = "R-"
LEGACY_PREFIXES = ("RES-",)  # users written by 0.1.0; nothing matches them any more, so they go as orphans

REASON_ENDED = "ended"
REASON_CANCELLED = "cancelled"
REASON_REGENERATED = "regenerated"
REASON_REVOKED = "revoked"
REASON_ORPHAN = "orphan"


def parse_dt(value: str) -> datetime:
    """ISO 8601 → aware UTC datetime. A naive value is taken as UTC (the API always sends Z)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Booking:
    id: str
    ref: str
    status: str
    start: datetime
    end: datetime
    entity_name: str | None
    guest_first_name: str | None
    guest_locale: str
    code_id: str | None
    pin: str | None = field(repr=False)  # never printed: the PIN is the one secret passing through HA
    valid_from: datetime | None
    valid_to: datetime | None
    recording_opt_in: bool
    recording_status: str
    updated_at: str | None

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def user_name(self) -> str:
        return f"{USER_PREFIX}{self.ref}"

    def code_window_open(self, now: datetime) -> bool:
        return bool(
            self.confirmed and self.code_id and self.pin and self.valid_from and self.valid_to
            and self.valid_from <= now < self.valid_to
        )

    def in_session(self, now: datetime) -> bool:
        return self.confirmed and self.start <= now < self.end


def parse_booking(raw: dict) -> Booking:
    """One feed record → Booking. Raises (KeyError, ValueError, TypeError) on a malformed record."""
    access = raw.get("access") or {}
    recording = raw.get("recording") or {}
    pin = access.get("pin")
    if pin is not None and not isinstance(pin, str):
        raise TypeError("access.pin must be a string")  # a number would have lost its leading zeros
    return Booking(
        id=str(raw["id"]),
        ref=str(raw["ref"]),
        status=str(raw["status"]),
        start=parse_dt(raw["start"]),
        end=parse_dt(raw["end"]),
        entity_name=raw.get("entity_name"),
        guest_first_name=raw.get("guest_first_name"),
        guest_locale=raw.get("guest_locale") or "pl",
        code_id=access.get("code_id"),
        pin=pin,
        valid_from=parse_dt(access["valid_from"]) if access.get("valid_from") else None,
        valid_to=parse_dt(access["valid_to"]) if access.get("valid_to") else None,
        recording_opt_in=bool(recording.get("opt_in")),
        recording_status=str(recording.get("status") or "none"),
        updated_at=raw.get("updated_at"),
    )


def parse_bookings(raws: list[dict]) -> tuple[list[Booking], list[str]]:
    """Parse a feed; malformed records are skipped and described in the second list (without PINs)."""
    out: list[Booking] = []
    bad: list[str] = []
    for raw in raws:
        try:
            out.append(parse_booking(raw))
        except (KeyError, ValueError, TypeError, AttributeError) as err:
            ident = raw.get("id") if isinstance(raw, dict) else None
            bad.append(f"{ident}: {type(err).__name__}: {err}")
    out.sort(key=lambda b: b.start)
    return out, bad


def desired_codes(bookings: list[Booking], now: datetime, revoked: set[str] | frozenset[str] = frozenset()) -> dict[str, Booking]:
    """code_id → booking for every code that should be on the lock right now."""
    return {b.code_id: b for b in bookings if b.code_window_open(now) and b.code_id not in revoked}


def removal_reason(code_id: str, record: dict, bookings: list[Booking], now: datetime, revoked: set[str] | frozenset[str]) -> str:
    """Why a code we hold is no longer wanted."""
    if code_id in revoked:
        return REASON_REVOKED
    booking = next((b for b in bookings if b.id == record.get("booking_id")), None)
    if booking is not None:
        if not booking.confirmed:
            return REASON_CANCELLED
        if booking.code_id and booking.code_id != code_id:
            return REASON_REGENERATED
    return REASON_ENDED


def next_boundary(bookings: list[Booking], now: datetime) -> datetime | None:
    """The next moment anything changes by the clock: a code window or a session opens or closes."""
    points = [
        t for b in bookings if b.confirmed
        for t in (b.valid_from, b.valid_to, b.start, b.end)
        if t is not None and t > now
    ]
    return min(points) if points else None


def current_booking(bookings: list[Booking], now: datetime) -> Booking | None:
    return next((b for b in bookings if b.in_session(now)), None)


def upcoming_booking(bookings: list[Booking], now: datetime) -> Booking | None:
    return next((b for b in bookings if b.confirmed and b.start > now), None)


# ---- camera presets ---------------------------------------------------------------------------

PRESET_PREFIX = "klient-"


def preset_name(entity_name: str | None) -> str | None:
    """The client's preset in the camera library: 'Studio XYZ sp. z o.o.' → 'klient-studio-xyz-sp-z-o-o'."""
    text = unicodedata.normalize("NFKD", (entity_name or "").replace("ł", "l").replace("Ł", "L"))
    slug = re.sub(r"[^a-z0-9]+", "-", text.encode("ascii", "ignore").decode().lower()).strip("-")[:48].strip("-")
    return f"{PRESET_PREFIX}{slug}" if slug else None


def preset_booking(bookings: list[Booking], now: datetime) -> Booking | None:
    """Whose settings belong on the cameras now: the session in progress, else the client about to
    start (their code already opens the door), else the one who has just finished and is packing."""
    live = [b for b in bookings if b.confirmed]
    for b in live:
        if b.start <= now < b.end:
            return b
    inside = [b for b in live if (b.valid_from or b.start) <= now < (b.valid_to or b.end)]
    coming = [b for b in inside if now < b.start]
    if coming:
        return min(coming, key=lambda b: b.start)
    return max(inside, key=lambda b: b.end) if inside else None


# ---- lock users -------------------------------------------------------------------------------

def user_at(users: list[dict], index: int | None) -> dict | None:
    if index is None:
        return None
    return next((u for u in users if u.get("user_index") == index), None)


def lock_name(user: dict | None) -> str:
    """The user's name as text. Locks pad or cut it: the U200 returns 'RES-FZKBE\x00' for 'RES-FZKBEP'."""
    return str((user or {}).get("user_name") or "").replace("\x00", "").strip()


def is_ours(user: dict) -> bool:
    return lock_name(user).startswith((USER_PREFIX, *LEGACY_PREFIXES))


def name_matches(name: str, ref: str) -> bool:
    """Does a name read back from the lock stand for R-<ref>, possibly cut short by the lock?"""
    wanted = f"{USER_PREFIX}{ref}"
    return len(name) >= len(USER_PREFIX) + 3 and wanted.startswith(name)


def record_matches_user(record: dict, user: dict | None) -> bool:
    """Is the lock user at the stored index really the one this record created?

    A named record must find its R-<ref> name (the lock may have cut its tail). A record whose
    naming step never succeeded matches only a nameless user, so a slot reused by someone else
    by hand is never touched.
    """
    if user is None:
        return False
    name = lock_name(user)
    if name_matches(name, str(record.get("ref"))):
        return True
    return not record.get("named") and name == ""


def orphan_users(users: list[dict], codes: dict[str, dict]) -> list[dict]:
    """Our users on the lock that no stored record accounts for (e.g. after a restored backup)."""
    known = {r.get("user_index") for r in codes.values() if r.get("user_index") is not None}
    return [u for u in users if is_ours(u) and u.get("user_index") not in known]
