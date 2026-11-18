# Aki Bookings for Home Assistant

Custom integration that reads studio bookings from the booking system (shirabe, `/api/ha/v1`),
puts each booking's door PIN on a Matter lock for its validity window, takes it off afterwards,
reports what happened and exposes the bookings as entities.

Home Assistant makes every request; the booking system never calls it. The booking system owns
the PINs and the windows. The integration only writes them to the lock and reports status.

## Requirements

- Home Assistant 2026.4 or newer (Matter lock user management).
- A lock paired **directly** over Matter, with user management (tested against Aqara U200).
- A `sha_…` token from the booking system: Admin → Settings → Home Assistant.

## Installation

Home Assistant Marketplace → custom repository `https://github.com/asquelt/aki-bookings-hass`,
type Integration. Then Settings → Devices & services → Add integration → **Aki Bookings**.

Releases: bump `version` in `manifest.json`, commit, tag `vX.Y.Z` and push. The Gitea repo mirrors
to GitHub, where the `release` workflow creates the release the Marketplace picks up.
An update needs a Home Assistant restart.

## Configuration

| Field | Notes |
| --- | --- |
| Address | `https://pero.ro`, without the API path. `https` only: the feed carries PINs |
| API token | `sha_…`, shown once by the booking system |
| Site access token | Staging only (`X-Site-Access-Token`) |
| Lock | The Matter lock entity |

Options:

| Option | Default | Notes |
| --- | --- | --- |
| Dry-run | **on** | Nothing is written to the lock and nothing is reported; planned operations are logged and fired as events with `dry_run: true` |
| Report in dry-run | off | Sends code status anyway. Only against a staging booking system |
| Polling period | 300 s | |
| Booking codes at once | 8 | Of the lock's 10 users; the rest stay free for hand-made codes |

Switching dry-run **on** removes the booking codes that are on the lock at that moment.

## How it works

Every poll (and exactly at each window edge) the integration compares what should be on the lock
with what the lock reports:

- A confirmed booking whose `valid_from ≤ now < valid_to` gets its PIN written as a new lock user
  named `RES-<ref>`. The code is reported `applied` only after the lock confirms it.
- A code is removed when its window ends, the booking is cancelled, its PIN is regenerated
  (new `code_id`), or the booking is missing from a successful feed.
- A failed feed removes nothing: the integration keeps working from the last good one, so codes
  still come off on time. Status reports wait in a queue and go out in order.
- Lock users without the `RES-` prefix are never touched. A `RES-` user the integration has no
  record of is removed as an orphan.
- A failed lock operation is retried after 30 s and 60 s; the third failure puts the code in the
  Error state (`failed` reported, event fired) and it is retried on every later pass.
  `duplicate` (the PIN already exists on the lock) fails at once: the booking system must issue a new PIN.
- While the lock is unavailable nothing is attempted or counted; work resumes when it returns.

## Entities

One device, "Rezerwacje studia":

| Entity | State | Attributes |
| --- | --- | --- |
| Current booking (sensor) | booking ref or `none` | `id`, `ref`, `entity_name`, `guest_first_name`, `guest_locale`, `start`, `end`, `code_status`, `recording_opt_in`, `recording_status`, `minutes_left` |
| Next booking (sensor) | booking ref or `none` | the same, with `minutes_to_start` |
| Booking active (binary sensor) | on during a session | `booking_id`, `ref` |
| Active codes (sensor) | codes on the lock | `refs`, `slot_limit`, `dry_run` |
| Last sync (sensor) | timestamp of the last good feed | `result`, `bookings_count`, `error_message`, `consecutive_failures`, `pending_reports` |
| Problem (binary sensor) | on when something needs attention | `reasons` (`code_failed`, `auth_failed`, `sync_stale`), `failed_codes`, `sync_error`, `lock_error` |
| Calendar | on during a session | one event per confirmed booking |

`code_status` is one of `none`, `scheduled`, `pending`, `applied`, `failed`, `revoked`, `removed`.
Booking attributes are excluded from the recorder. The PIN is never in a state, an attribute, an
event or a log line; it lives only in the integration's file under `.storage`.

## Events

| Event | Data |
| --- | --- |
| `aki_bookings_code_applied` | `booking_id`, `ref`, `code_id`, `valid_from`, `valid_to`, `dry_run` |
| `aki_bookings_code_removed` | `booking_id`, `ref`, `code_id`, `reason` (`ended`, `cancelled`, `regenerated`, `revoked`, `orphan`), `dry_run` |
| `aki_bookings_code_failed` | `booking_id`, `ref`, `code_id`, `operation` (`apply`, `remove`), `error`, `dry_run` |
| `aki_bookings_sync_failed` | `error`, `consecutive_failures` |

All four appear in the logbook.

## Actions

| Action | Parameters | What it does |
| --- | --- | --- |
| `aki_bookings.sync_now` | | Fetch and reconcile now |
| `aki_bookings.reconcile_lock` | | Read the lock and fix differences, without the API |
| `aki_bookings.revoke_code` | `booking_id` | Take the code off now; it returns only with a regenerated PIN |
| `aki_bookings.set_recording_opt_in` | `booking_id`, `opt_in` | Record the upload choice in the booking system |
| `aki_bookings.report_recording` | `booking_id`, `status`, `url`, `expires_at`, `error` | Report upload progress (`uploading`, `ready`, `failed`, `empty`) |

`booking_id` accepts the booking id or its 6-character ref.

## Not here yet

The recording pipeline (take journal, nightly upload through the camera bridges, download page,
clean-up) is not implemented; only the two reporting actions above exist.

## Development

```
python3 -m venv .venv && .venv/bin/pip install pytest-homeassistant-custom-component
.venv/bin/pytest
```

The tests run the whole integration against a fake lock (real `matter.*` action names) and a
fake booking API.
