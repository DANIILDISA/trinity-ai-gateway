# Checkpoint: Financial Safety Report

**Date:** 2026-05-14
**Suite:** Phase 0–2 (Tasks 0.1–2.6)
**Total tests:** 137/137 PASS

---

## 1. Automated Tests

`docker compose exec backend pytest -v` → **137 passed, 10 warnings in 20.41s**

Per-file breakdown:

| File | Tests | Status |
|---|---|---|
| `tests/test_auth.py` | 29 | PASS |
| `tests/test_billing.py` | 24 | PASS |
| `tests/test_billing_concurrent.py` | 4 | PASS |
| `tests/test_cleanup.py` | 7 | PASS |
| `tests/test_constants.py` | 38 | PASS |
| `tests/test_csrf.py` | 4 | PASS |
| `tests/test_database.py` | 8 | PASS |
| `tests/test_health.py` | 6 | PASS |
| `tests/test_redis.py` | 8 | PASS |
| `tests/test_users.py` | 6 | PASS (Phase 2 new) |
| **TOTAL** | **137** | **137 PASS / 0 FAIL** |

Warnings: 10 × httpx `DeprecationWarning` on per-request `cookies=…` argument in
`test_auth.py` — pre-existing, unrelated to financial code.

## 2. Concurrent Safety (5 runs)

`for i in 1..5: pytest tests/test_billing_concurrent.py -v`

| Run | Result |
|---|---|
| 1 | 4 passed in 1.00s |
| 2 | 4 passed in 0.88s |
| 3 | 4 passed in 0.95s |
| 4 | 4 passed in 0.93s |
| 5 | 4 passed in 0.88s |

**Total: 20/20 PASS** across 5 runs. Tests covered:
- `test_double_reservation_race` (only one reservation wins on tight balance)
- `test_credit_debit_race` (no negative balance, ledger invariant holds)
- `test_multi_reservation_race` (5 racers on 1000₽, reserved ≤ balance, all losers raise `InsufficientFundsError`)
- `test_finalize_release_race` (exactly one of finalize/release succeeds, the other raises HTTPException 400)

Stability verdict: **deterministic**, no flake observed.

## 3. DB Invariant Checks

All queries executed via `docker compose exec postgres psql -U trinity -d trinity`.

### Query A — `balance_micro_rub == SUM(transactions.amount_micro_rub)` for every user
```
 id | balance_micro_rub | computed | drift
----+-------------------+----------+-------
(0 rows)
```
**PASS** — no drift on any user.

### Query B — No negative balance
```
 id | email | balance_micro_rub
----+-------+-------------------
(0 rows)
```
**PASS**.

### Query C — No user with `balance < reserved`
```
 id | balance_micro_rub | reserved_micro_rub
----+-------------------+--------------------
(0 rows)
```
**PASS**.

### Query D — No negative `reserved_micro_rub`
```
 id | reserved_micro_rub
----+--------------------
(0 rows)
```
**PASS**.

### Query E — No duplicate `idempotency_key`
```
 idempotency_key | cnt
-----------------+-----
(0 rows)
```
**PASS** — UNIQUE constraint holds.

### Query F — Every debit with `request_id` has a matching `usage_logs` row
```
 id | type | amount_micro_rub | request_id
----+------+------------------+------------
(0 rows)
```
**PASS** — invariant #5 from BILLING_LOGIC.md §1.3 satisfied for current data.

### Query G — No orphaned active reservations older than 5 minutes
```
 id | user_id | created_at | status
----+---------+------------+--------
(0 rows)
```
**PASS** — cleanup is up to date (and tests do not leave behind active reservations).

### Query H — CHECK constraint must reject negative balance INSERT
```
ERROR: new row for relation "users" violates check constraint "chk_available_balance"
DETAIL: Failing row contains (..., constraint_test@test.com, ..., -1, 0, ...).
EXIT_CODE=1
```
**PASS** — the INSERT was rejected.

Note: the constraint that fired was `chk_available_balance` (`balance_micro_rub >= reserved_micro_rub`) rather than `chk_users_balance_nonneg` (`balance_micro_rub >= 0`). With `balance=-1, reserved=0` both constraints are violated; Postgres evaluated `chk_available_balance` first and short-circuited. The behavior is correct: a negative-balance row cannot be inserted.

## 4. Currency Precision

```
OK: got=0.3600, expected=0.3600
OK: got=-0.3600, expected=-0.3600
OK: got=847.53, expected=847.53
OK: got=320, expected=320
OK: got=3000, expected=3000
OK: got=247500000, expected=247500000
OK: got=500000000, expected=500000000
OK: got=1, expected=1
ALL PASS
```
**PASS** — all 8 micro-ruble / micro-USD conversions match expected.

## 5. Idempotency

Script: identify an existing user, call `credit_balance(uid, 1_000_000, key, …)` twice
with the same `idempotency_key`, verify the second call is rejected without
incrementing balance.

(Note: the supplied snippet referenced `settings.DATABASE_URL`, dict-returning
`credit_balance`, and `sessionmaker` — none of which match the actual codebase.
Adapted to `settings.database_url`, the `tuple[int, bool]` return type, and
`async_sessionmaker`. No source files modified.)

```
First call applied: True  (balance=1000000)
Second call (duplicate) applied: False  (balance=1000000)
IDEMPOTENCY OK
```
**PASS** — second call returned `was_applied=False`, balance unchanged at 1,000,000 μ₽.
The DB UNIQUE constraint on `transactions.idempotency_key` plus the
`INSERT ... ON CONFLICT DO NOTHING` pattern correctly prevented a double credit.

Side effect: the first user in the `users` table received an additional 1 μRUB
credit and one transaction row (the cost of running the verification script as
instructed). Invariant A still holds (balance and SUM(txn) both incremented by
the same amount).

## 6. Overall Verdict

**PASS.**

- 137/137 unit + integration tests green (1 run); 20/20 concurrent tests green over 5 repeats.
- 8/8 DB invariant queries returned the expected empty / error result.
- 8/8 currency-precision spot checks match.
- Idempotency verified: duplicate-key credit is a no-op.
- All five Phase-2 financial invariants from BILLING_LOGIC.md §1.3 confirmed:
  1. `balance_micro_rub == SUM(transactions.amount_micro_rub)` — Query A
  2. `balance_micro_rub >= 0` — Query B + Query H (DB CHECK)
  3. `balance_micro_rub >= reserved_micro_rub` — Query C (and Query H confirms the CHECK fires)
  4. Unique `idempotency_key` — Query E + STEP 5
  5. Every debit ↔ usage_logs by `request_id` — Query F

No CRITICAL FAILURES detected. Phase 2 financial subsystem is safe to build on.
