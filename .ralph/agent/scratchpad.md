# Scratchpad — phase 08

## 2026-11-04 Planner: review inbox
Two inbox notes (`2026-11-04-recording-copy.md`, `2026-11-04-recording-copy-lock.md`) carry the SAME patch
(`cmp` identical) and the same two findings. The second note asks to land patch + fixes as one FIX task, so both
notes map to one task P08-FIX-1 (inserted at the top of plan/phase-08.md). After approval the Reviewer appends BOTH
file names to `.ralph/review-inbox/DONE`. `git apply --check -3` of the patch on HEAD succeeds cleanly.

## Plan P08-FIX-1
Goal: land the maintainer recording-copy patch (copy lock `call:{id}:reccopy`, `RecordingBusy`, `TelnyxClient.get_recording`,
FakeTelnyx `GET /v2/recordings/{id}`, 403/404 link refresh, spec rules 4/5/7) and fix both findings.

### Step 1 — apply the patch
`git apply -3 .ralph/review-inbox/2026-11-04-recording-copy.patch` (adds `packages/va_telnyx/tests/test_get_recording.py`).
Do NOT edit/delete the inbox files. Read the applied diff of `recording.py`, `recording_rig.py`, `test_recording_activity*.py`,
`apps/worker/src/va_worker/activities.py` (wires `RecordingCopyLock(redis)`), errors.py (`RECORDING_BUSY`).

### Step 2 — finding 1 (blocker): per-recording object key
- `packages/va_common/src/va_common/storage.py`: add
  `recording_copy_key(org_id, call_id, created_at, recording: str) -> str` =
  `{org_id}/{yyyy}/{mm}/{call_id}.{sha256(recording)[:16]}.wav` (same `_month_prefix`, same tz validation as
  `recording_key`). Keep `recording_key` (still used for docs/tests? — if nothing in src uses it after the change, keep it
  only if other code/tests rely on it; grep. Currently only recording.py + tests use it; replace in recording.py; keep
  the function + its test since specs name it, or remove both and update the spec — Builder: keep it, it is still the
  documented "base" name and harmless). Export in `__all__`. Unit test in `packages/va_common/tests/test_storage.py`:
  deterministic, differs per recording identity, same month prefix, refuses naive datetimes.
- `recording.py` `_copy`: `key = recording_copy_key(inp.org_id, inp.call_id, call.created_at, owner)` (pass `owner`
  down from `run`). Same owner → same key → a redelivery uploads identical bytes to the same object (fine).
- `_record` returning False (key already set): if the row's key != this run's key, delete this run's just-uploaded
  object (`ObjectStore.delete(bucket, key)`; failure logged `recording.orphan_delete_failed`, never raises) and return
  `already_stored` describing the ROW (re-load, `_already_stored(call, saved)`), and never delete the carrier copy. If
  the row's key == this key (same owner won), do NOT delete (it is the stored object) → `already_stored`.
  Implement by making `_record` return the row's current key on loss (UPDATE ... RETURNING; on no row, SELECT
  recording_s3_key, recording_duration_ms in the same tenant session).
- Module docstring + spec: lock is now only a duplicate-work saver; correctness comes from per-recording keys.

### Step 3 — finding 2: busy retry covers the lock TTL
- `RecordingCopyLock.acquire` returns `int` remaining PTTL ms when refused (Lua: `return redis.call('PTTL', KEYS[1])`
  on refusal; return -1 when taken... simpler: return a small dataclass/`LockAttempt(taken: bool, retry_after_ms: int)`).
  Lua refused branch returns the PTTL (>0); taken returns 0 → map to `taken` / `retry_after_ms`.
- `_lock`: on refusal raise `ApplicationError(BUSY_MESSAGE, type=RECORDING_BUSY, next_retry_delay=timedelta(ms=max(pttl,
  1000) + 1000 jitter-free margin))`. Add `errors.retryable(message, error_type, *, next_retry_delay=None)` param rather
  than constructing ApplicationError inline. PTTL ≤ 0 (expired between calls / no TTL) → 1 s.
- With `maximum_attempts=5` and delays ≈ remaining TTL (≤ 6 min), the budget covers a crashed holder; PostCallWorkflow
  30 min budget still fits. Note in spec rule 4.

### Step 4 — specs
- specs/06 §11 Decision P05-T14: amend rule 1 (raw copy key is `{org}/{yyyy}/{mm}/{call_id}.{h}.wav`, h = first 16 hex of
  sha256 of the recording identity; `calls.recording_s3_key` names it; readers always use the row's key) and step 3
  text at ~line 664 (add "(key per recording: rule 1)"), rule 4 (per-recording key makes the object match the row; the
  losing run deletes its own object unless the keys are equal; lock = duplicate-work saver; busy retry delay = lock
  PTTL). Add `> Decision (2026-11-04, P08-FIX-1): ...` note at the end of the P05-T14 decision.
- AGENTS.md: patch adds a line; extend it with the new key builder + busy delay.

### Tests (packages/va_workflows/tests/test_recording_activity.py, use recording_rig)
1. `test_two_different_recordings_without_the_lock_keep_object_and_row_in_step`: broken Redis lock (copy lock whose
   redis raises — rig likely has `broken_redis()` from telephony_rig), run two `store_recording` for different
   recording ids with different WAV bytes (`serve_recording`) concurrently (asyncio.gather) → exactly one `stored`, one
   `already_stored`; row key's object bytes in MemoryS3 == the winner's WAV; loser's key absent (deleted); the loser's
   carrier copy not deleted (FakeTelnyx DELETE not called for it).
2. `test_a_losing_run_with_the_same_recording_keeps_the_object`: same recording run twice sequentially with the lock
   bypassed after the first committed... (simplest: call `_record`-loss path via two runs of the same owner with broken
   lock, gathered) → object present, row key == that key.
3. `test_busy_lock_retries_after_its_remaining_ttl`: pre-set `call:{id}:reccopy` to a foreign owner with PX 200_000 in
   fakeredis → run raises `ApplicationError` type `RecordingBusy`, `next_retry_delay` within [PTTL-1s, PTTL+2s]; and
   with a key without TTL / expired → ≥ 1 s.
4. Existing key assertion in test_recording_activity.py:79 → `recording_copy_key(..., "rec:<id>")`; minio test likewise.
5. `MemoryS3` needs `delete_object` (record deletes) if absent.

### Gates
`uv run pytest packages/va_workflows packages/va_telnyx packages/va_sim/tests/telnyx/test_recordings.py packages/va_common/tests/test_storage.py apps/worker -q`,
then `make check` (run in background with heartbeat; ~25 min; PATH must include /home/ralph/.local/bin). No e2e needed
(no call/media flow change), but recording key change touches call artifacts reads via row key only — grep
`apps/api` for `recording_key` to confirm none (confirmed by Planner: none).

### Risks
- MinIO integration tests compare keys; update them.
- `next_retry_delay` is honoured by Temporal even above `maximum_interval` (30 s) — it overrides the computed interval.
- Don't delete the object when the row key equals ours (same-owner race).

## Build P08-FIX-1 (2026-11-04)
- Applied inbox patch (`git apply -3`, staged), then:
- `va_common.storage.recording_copy_key(org, call, created_at, recording)` → `{org}/{yyyy}/{mm}/{call}.{sha256(owner)[:16]}.wav`.
- recording.py: `_copy(..., owner)` uploads to the per-recording key; `_record` returns `_StoredKey | None` (row's key on
  loss); `_lost` deletes its own object unless keys equal (`recording.orphan_deleted` / `orphan_delete_failed`), returns
  `already_stored` with the row key. `RecordingCopyLock.acquire` → `CopyLockAttempt(taken, remaining_ms)` (Lua returns
  `{taken, PTTL}`); busy raises `retryable(..., next_retry_delay=PTTL+1s)` (`BUSY_RETRY_MARGIN_MS`).
- errors.retryable gained `next_retry_delay=`. MemoryS3 gained `delete_object`, `deleted`, `fail_deletes`.
- Tests: busy delay follows PTTL; no-TTL lock → 1 s; two different recordings racing (barrier in uploads, broken lock):
  object bytes == winner, loser key deleted, loser carrier copy kept; same recording twice keeps the object; failed
  orphan delete logged.
- Specs: 06 §11 step 3, rule 1 + rule 4 amended, Decision P08-FIX-1 note; specs/18 §1 step 3 key. AGENTS.md line updated.

## Review P08-FIX-1 (2026-11-04)
Approved. make check exit=0 (12641 passed, 7 skipped, 99% cov, lint/typecheck/placeholders clean). Diff checked against
both inbox findings: per-recording key + orphan delete (unless equal key), busy delay = PTTL + 1 s (≤ 4 retries × ≤ 6 min
fits the 30 min post-call budget); specs 03/06/18/30 consistent. Both inbox notes appended to DONE.
