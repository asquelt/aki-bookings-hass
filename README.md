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
  named `R-<ref>` (8 characters: the Aqara U200 keeps only 9 of Matter's 10). The code is reported `applied` only after the lock confirms it.
- A code is removed when its window ends, the booking is cancelled, its PIN is regenerated
  (new `code_id`), or the booking is missing from a successful feed.
- A failed feed removes nothing: the integration keeps working from the last good one, so codes
  still come off on time. Status reports wait in a queue and go out in order.
- Lock users without the `R-` prefix are never touched (`RES-` from 0.1.0 counts as ours too). Such a user the integration has no
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
| Recordings (sensor) | sessions that still need something | see [Recordings](#recordings) |

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

## Recordings

Off by default (options → Recordings). Needs the `a74_bridge` integration, whose bridges do the
actual transfer from the camera cards and the console disk straight into an S3-compatible bucket
(Cloudflare R2). Home Assistant moves no media; it decides what goes where and when.

**Only with the client's opt-in.** A booking is uploaded only when the booking system says
`recording.opt_in: true`. The choice is read fresh right before the upload starts, and reporting
`uploading` locks it.

What happens, by itself:

1. **Take journal.** Every on/off of the recording-mode entity is a take, tagged with the booking
   whose code window was open. The journal is kept even while the pipeline is off.
2. **After the day's last session** (nobody in, recording mode off, at least 60 minutes to the next
   code window) the cards and the console disk are listed once and every finished session is sorted out:
   - opt-in and files: reported `uploading`, then sent: cameras first (they lose remote control
     while sending), console ISO after. Up to three tries a night; files already in the bucket are skipped;
   - opt-in but no files: reported `empty`;
   - no opt-in but files: event `aki_bookings_recordings_left`, repeated daily until the operator decides;
   - no opt-in, no files: event `aki_bookings_recordings_none`.
3. **A file belongs to a booking** when it was recorded during one of its takes (±10 s) or, outside
   any take, inside the booking's code window. Any other file belongs to nobody and is never
   uploaded: it may be the previous client's.
4. **Interruption.** A camera upload is cancelled 18 minutes before the next code window, and at
   once when recording mode comes on or a code window opens. It resumes when the conditions return.
5. **When everything is in the bucket** the sizes are checked against the sources, `index.html`
   (one signed link per file, plus a "Download all" button: Chromium browsers save every file into a
   folder the client picks, others download them one by one) and `links.txt` are written, and the signed link to the page is
   reported as `ready` with its expiry. The link is never in a state, an attribute, an event or a log.
6. **After the download time** (1–7 days, default 7) the booking's folder is deleted from the bucket.
7. **Three nights without progress** report `failed`; the upload then waits for the operator.

What the booking system refuses is final for the day, and the cards are not listed again until tomorrow:

- `409 recording_delivered` (a link was already delivered; only a new `ready` may follow): the
  session is closed, nothing is sent.
- `409 recording_not_opted_in` (the client withdrew the choice since the last feed): nothing is sent.
- `404`: the booking was cancelled before its start; the session is closed.

A booking cancelled **after** its session began is handled like any other. The feed carries no
cancellation time, so "began" means: the booking was already in the register, or the journal has a
take of it, or its `updated_at` is later than its start.

The register behind this holds no client data: booking id, ref, times, the choice, file names.

| Option | Default | Notes |
| --- | --- | --- |
| Recording pipeline | off | The journal is kept either way |
| Recording-mode entity | `input_boolean.tryb_nagrywania` | On while a take is recorded |
| Storage endpoint, bucket, keys | | Read, write and delete rights on the bucket the bridges upload to |
| Folder in the bucket | `nagrania` | Each booking gets `<folder>/<ref>-<random>` |
| Days to download | 7 | Signed links cannot live longer |

The Recordings sensor counts sessions that still need something. Its attributes: `sessions` (each
with state `waiting`, `uploading`, `ready`, `failed` or `left`, files, files sent, bytes, expiry),
`active` and `phase` (the upload running now), `takes` (the last 50 takes of the journal; a take is one on/off of recording mode, not a file: `start`,
`stop`, `seconds`, booking `ref`), `open_take` (recording mode is on now), `enabled`, `last_error`.
The whole journal and register are in the downloadable diagnostics.

More events: `aki_bookings_recordings_ready`, `_failed`, `_empty`, `_left`, `_none`,
`_unassigned` (files outside any booking), `_deleted`.

More actions: `aki_bookings.resolve_recordings` (`booking_id`, `action`: `upload` opts in on the
client's behalf, `keep` asks again tomorrow) and `aki_bookings.run_recordings` (one pass now; every
safety condition still applies).

Not here yet: deleting files from the camera cards and the console disk (the bridges cannot do it),
so `resolve_recordings` with `delete` is refused and uploaded files stay on the sources.

## Development

```
python3 -m venv .venv && .venv/bin/pip install pytest-homeassistant-custom-component
.venv/bin/pytest
```

The tests run the whole integration against a fake lock (real `matter.*` action names) and a
fake booking API.
