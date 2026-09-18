# Development-only failed attempts

These are implementation diagnostics, not experiment results. Each failed
attempt created UUID-suffixed resources and verified database/role cleanup.

- Initial fixture seed ran after `FORCE ROW LEVEL SECURITY`; owner insert was
  rejected, as expected. Seed order was corrected.
- The first schedule lacked the UPDATE privilege required by
  `SELECT ... FOR UPDATE`; the reader failed closed before the barrier.
- A `WITH CHECK (false)` lock policy made the positive locking read empty; it
  was replaced by column-level UPDATE on harmless `lock_token` plus protected
  column privilege checks.
- A positive probe was initially placed after revocation; it was moved before
  the schedule to prevent vacuous repair evidence.

The passing run is only `../results.json`.
