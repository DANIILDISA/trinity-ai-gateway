"""
Trinity API Load Tests
======================
Tests the Trinity AI gateway backend (FastAPI on port 8000).

Personas
--------
AnonymousUser       Public endpoints (health, models listing).
AuthenticatedUser   Registered users – profile, conversations, read/write.
AdminUser           Admin dashboard reads.  Set LT_ADMIN_EMAIL + LT_ADMIN_PASSWORD.

Load shapes  (LOAD_SHAPE env var)
----------------------------------
ramp    (default)  5 → 10 → 25 → 50 users, hold 90 s, ramp down.  ~6 min total.
steady             Flat 25 users for 5 min.
spike              10 baseline → 100 spike for 60 s → recover → hold.
stress             Keeps doubling to 200 users to find the breaking point.

No permanent deletes are issued.  Archive operations are immediately restored.
Each virtual user registers its own account; those records remain in the DB.

Usage
-----
    # Interactive web UI:
    locust -f locustfile.py --host http://localhost:8000

    # Headless ramp (generates report.html + report_*.csv):
    locust -f locustfile.py --host http://localhost:8000 --headless \\
           --html report.html --csv report

    # Different shape:
    LOAD_SHAPE=spike locust -f locustfile.py --host http://localhost:8000 --headless \\
           --html report_spike.html --csv report_spike
"""

import os
import random
import uuid
from typing import Optional

from locust import HttpUser, LoadTestShape, between, task
from locust.exception import StopUser

# ── Configuration ──────────────────────────────────────────────────────────────

ADMIN_EMAIL: str = os.getenv("LT_ADMIN_EMAIL", "")
ADMIN_PASSWORD: str = os.getenv("LT_ADMIN_PASSWORD", "")
LOAD_SHAPE: str = os.getenv("LOAD_SHAPE", "ramp").lower()
LT_MAX_USERS: int = int(os.getenv("LT_MAX_USERS", "50"))

# Fallback model used when /models returns nothing usable.
_FALLBACK_MODEL = "openai/gpt-4o-mini"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _csrf(response) -> str:
    """Extract CSRF token from a response (header takes priority, then cookie)."""
    return response.headers.get("X-CSRF-Token", "")


def _pick_model(client) -> str:
    """Fetch /models and return the first available model id."""
    try:
        r = client.get("/api/v1/models", name="[setup] GET /models")
        if r.ok:
            data = r.json()
            items = data if isinstance(data, list) else (
                data.get("models") or data.get("items") or []
            )
            if items:
                return items[0].get("id", _FALLBACK_MODEL)
    except Exception:
        pass
    return _FALLBACK_MODEL


# ── User classes ───────────────────────────────────────────────────────────────


class AnonymousUser(HttpUser):
    """Unauthenticated users browsing the public API."""

    weight = 1
    wait_time = between(0.5, 2)

    def on_start(self) -> None:
        self._csrf_token = ""
        r = self.client.get("/api/v1/health", name="GET /health")
        if r.ok:
            self._csrf_token = _csrf(r)

    # ── Tasks ──

    @task(5)
    def health(self) -> None:
        r = self.client.get("/api/v1/health", name="GET /health")
        if r.ok:
            self._csrf_token = _csrf(r) or self._csrf_token

    @task(3)
    def list_models(self) -> None:
        self.client.get("/api/v1/models", name="GET /models")


class AuthenticatedUser(HttpUser):
    """Typical registered user – registers once, then performs read/write ops."""

    weight = 7
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.token: Optional[str] = None
        self.csrf_token: str = ""
        self.conversation_ids: list[str] = []
        self.model_id: str = _FALLBACK_MODEL

        # Bootstrap: get CSRF + discover a valid model id.
        r = self.client.get("/api/v1/health", name="[setup] GET /health")
        if r.ok:
            self.csrf_token = _csrf(r)

        self.model_id = _pick_model(self.client)

        uid = uuid.uuid4().hex[:12]
        email = f"lt_{uid}@example.com"
        password = "LoadTest1!"

        # Register (record stays in DB – no deletion per test policy).
        rr = self.client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": password,
                "display_name": f"LT-{uid[:8]}",
            },
            headers=self._mh(),
            name="POST /auth/register",
        )
        if not rr.ok:
            raise StopUser()
        self.csrf_token = _csrf(rr) or self.csrf_token

        # Login.
        rl = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
            headers=self._mh(),
            name="POST /auth/login",
        )
        if not rl.ok:
            raise StopUser()
        self.csrf_token = _csrf(rl) or self.csrf_token
        self.token = rl.json().get("access_token")
        if not self.token:
            raise StopUser()

    # ── Header helpers ──

    def _bh(self) -> dict:
        """Base headers (CSRF only)."""
        return {"X-CSRF-Token": self.csrf_token} if self.csrf_token else {}

    def _mh(self) -> dict:
        """Mutating request headers (CSRF)."""
        return self._bh()

    def _ah(self) -> dict:
        """Authenticated request headers (CSRF + Bearer)."""
        h = self._bh()
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _refresh_csrf(self, response) -> None:
        self.csrf_token = _csrf(response) or self.csrf_token

    # ── Read tasks (high frequency) ──

    @task(6)
    def get_profile(self) -> None:
        r = self.client.get("/api/v1/users/me", headers=self._ah(), name="GET /users/me")
        if r.status_code == 401:
            raise StopUser()

    @task(5)
    def list_models(self) -> None:
        self.client.get("/api/v1/models", name="GET /models")

    @task(5)
    def list_conversations(self) -> None:
        self.client.get(
            "/api/v1/chat/conversations",
            headers=self._ah(),
            name="GET /chat/conversations",
        )

    @task(2)
    def list_transactions(self) -> None:
        self.client.get(
            "/api/v1/users/me/transactions",
            headers=self._ah(),
            name="GET /users/me/transactions",
        )

    @task(2)
    def list_notifications(self) -> None:
        self.client.get(
            "/api/v1/users/me/notifications",
            headers=self._ah(),
            name="GET /users/me/notifications",
        )

    @task(1)
    def image_gallery(self) -> None:
        self.client.get(
            "/api/v1/images/gallery",
            headers=self._ah(),
            name="GET /images/gallery",
        )

    # ── Write tasks (lower frequency) ──

    @task(3)
    def create_conversation(self) -> None:
        r = self.client.post(
            "/api/v1/chat/conversations",
            json={"model_id": self.model_id, "title": f"lt-{uuid.uuid4().hex[:8]}"},
            headers=self._ah(),
            name="POST /chat/conversations",
        )
        self._refresh_csrf(r)
        if r.status_code == 201:
            cid = r.json().get("id")
            if cid:
                self.conversation_ids.append(cid)

    @task(1)
    def get_single_conversation(self) -> None:
        if not self.conversation_ids:
            return
        cid = random.choice(self.conversation_ids)
        self.client.get(
            f"/api/v1/chat/conversations/{cid}",
            headers=self._ah(),
            name="GET /chat/conversations/{id}",
        )

    @task(1)
    def rename_conversation(self) -> None:
        if not self.conversation_ids:
            return
        cid = random.choice(self.conversation_ids)
        r = self.client.patch(
            f"/api/v1/chat/conversations/{cid}",
            json={"title": f"renamed-{uuid.uuid4().hex[:6]}"},
            headers=self._ah(),
            name="PATCH /chat/conversations/{id}",
        )
        self._refresh_csrf(r)

    @task(1)
    def archive_restore(self) -> None:
        """Soft-delete then immediately restore – no permanent data loss."""
        if not self.conversation_ids:
            return
        cid = self.conversation_ids[0]
        r = self.client.delete(
            f"/api/v1/chat/conversations/{cid}",
            headers=self._ah(),
            name="DELETE /chat/conversations/{id} [soft]",
        )
        self._refresh_csrf(r)
        if r.status_code in (200, 204):
            r2 = self.client.post(
                f"/api/v1/chat/conversations/{cid}/restore",
                headers=self._ah(),
                name="POST /chat/conversations/{id}/restore",
            )
            self._refresh_csrf(r2)


class AdminUser(HttpUser):
    """Admin dashboard reads.  Disabled if LT_ADMIN_EMAIL/LT_ADMIN_PASSWORD are unset."""

    weight = 1
    wait_time = between(2, 5)

    def on_start(self) -> None:
        if not ADMIN_EMAIL or not ADMIN_PASSWORD:
            raise StopUser()

        self.token: Optional[str] = None
        self.csrf_token: str = ""

        r = self.client.get("/api/v1/health", name="[admin-setup] GET /health")
        if r.ok:
            self.csrf_token = _csrf(r)

        rl = self.client.post(
            "/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            headers={"X-CSRF-Token": self.csrf_token},
            name="[admin-setup] POST /auth/login",
        )
        if not rl.ok:
            raise StopUser()
        self.csrf_token = _csrf(rl) or self.csrf_token
        self.token = rl.json().get("access_token")
        if not self.token:
            raise StopUser()

    def _h(self) -> dict:
        h: dict = {}
        if self.csrf_token:
            h["X-CSRF-Token"] = self.csrf_token
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @task(4)
    def dashboard(self) -> None:
        self.client.get(
            "/api/v1/admin/dashboard", headers=self._h(), name="GET /admin/dashboard"
        )

    @task(3)
    def list_users(self) -> None:
        self.client.get(
            "/api/v1/admin/users?page=1&per_page=20",
            headers=self._h(),
            name="GET /admin/users",
        )

    @task(2)
    def list_alerts(self) -> None:
        self.client.get(
            "/api/v1/admin/alerts", headers=self._h(), name="GET /admin/alerts"
        )

    @task(2)
    def revenue_by_model(self) -> None:
        self.client.get(
            "/api/v1/admin/dashboard/revenue-by-model",
            headers=self._h(),
            name="GET /admin/dashboard/revenue-by-model",
        )

    @task(1)
    def tariffs(self) -> None:
        self.client.get(
            "/api/v1/admin/tariffs", headers=self._h(), name="GET /admin/tariffs"
        )


# ── Load shapes ────────────────────────────────────────────────────────────────
# Each stage: (cumulative_end_seconds, target_users, spawn_rate_per_second)
# spawn_rate=0 means "hold" (don't add/remove users).

def _custom_stages(max_users: int) -> list[tuple[int, int, int]]:
    """Build a ramp-up shape for any target user count.

    Ramp speed is kept conservative (≤ 10 users/s) so bcrypt registration
    hashes never saturate the CPU in a sudden burst.  Holds at peak for 4 min.
    """
    milestones = [u for u in [50, 100, 250, 500, 1000, 2000] if u < max_users]
    milestones.append(max_users)
    stages: list[tuple[int, int, int]] = []
    t = 0
    for users in milestones:
        prev = stages[-1][1] if stages else 0
        delta = users - prev
        rate = max(2, min(10, delta // 20))   # 5–10 users/s, at least 2
        duration = max(60, delta // rate)
        t += duration
        stages.append((t, users, rate))
    hold_end = t + 240                        # hold peak 4 min
    stages.append((hold_end, max_users, 0))
    stages.append((hold_end + 60, 0, 50))     # fast ramp-down
    return stages


_STAGES: dict[str, list[tuple[int, int, int]]] = {
    # Gradual ramp to find stable throughput.  ~6 min.
    "ramp": [
        (30,  5,   2),
        (90,  10,  2),
        (150, 25,  3),
        (300, 50,  4),   # hold at 50 for 2.5 min
        (360, 0,   10),  # ramp down
    ],
    # Sustained constant load for steady-state latency measurement.  ~7 min.
    "steady": [
        (30,  25, 5),
        (390, 25, 0),    # hold 6 min
        (420, 0,  10),
    ],
    # Sudden traffic spike to test recovery behaviour.  ~6 min.
    "spike": [
        (30,  10,  5),
        (60,  100, 50),  # spike
        (120, 100, 0),   # hold spike 60 s
        (180, 10,  50),  # recover
        (330, 10,  0),   # hold at baseline
        (360, 0,   10),
    ],
    # Escalating stress test to locate the saturation point.  ~8 min.
    "stress": [
        (60,  10,  2),
        (120, 25,  3),
        (180, 50,  5),
        (240, 100, 10),
        (300, 150, 10),
        (420, 150, 0),   # hold peak 2 min
        (480, 0,   20),
    ],
    # Configurable high-load shape.  Set LT_MAX_USERS to the desired peak.
    "custom": _custom_stages(LT_MAX_USERS),
}


class TrinityLoadShape(LoadTestShape):
    """Selects the active stage list from LOAD_SHAPE (default: ramp)."""

    def tick(self):  # type: ignore[override]
        t = self.get_run_time()
        stages = _STAGES.get(LOAD_SHAPE, _STAGES["ramp"])
        for end_s, users, rate in stages:
            if t < end_s:
                return users, rate
        return None  # signals Locust to stop the test
