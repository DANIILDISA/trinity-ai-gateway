"""Client for the sandbox code-execution service (epic tr-s3y1, Phase 1).

Thin async wrapper over the sandbox `/execute` HTTP endpoint. `conversation_id`
is accepted now (unused in stateless Phase 1) so the interface is stable for the
per-conversation kernels added in Phase 3.
"""
import httpx
from pydantic import BaseModel, Field

from app.config import settings


class ExecutionResult(BaseModel):
    status: str
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool
    detail: str | None = None
    # Phase 3: the conversation's kernel was reset (timeout/crash/eviction) — the
    # persistent state is gone; the next run starts fresh.
    reset: bool = False
    # Phase 4-ready: rich outputs (e.g. {"type":"image/png","data":...}); empty now.
    results: list = Field(default_factory=list)
    # B3 (tr-f63e): the manager has no kernel seeded for this seed_hash → the backend must
    # retry the call with full file bytes.
    needs_seed: bool = False


class SandboxClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 35.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = (base_url or settings.sandbox_url).rstrip("/")
        self._timeout = timeout
        self._client = client  # injectable for tests

    async def execute(
        self,
        *,
        code: str,
        conversation_id: str | None = None,
        timeout: int | None = None,
        files: list[dict] | None = None,
        seed_hash: str | None = None,
        http_timeout: float | None = None,
    ) -> ExecutionResult:
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            # B3 (tr-f63e): a probe may pass a short http_timeout so an overloaded sandbox
            # can't hang it for the full client timeout; None keeps the client default.
            kwargs = {} if http_timeout is None else {"timeout": http_timeout}
            resp = await client.post(
                f"{self._base}/execute",
                json={
                    "code": code,
                    "timeout": timeout,
                    "conversation_id": conversation_id,
                    "files": files or [],
                    "seed_hash": seed_hash,
                },
                **kwargs,
            )
            resp.raise_for_status()
            return ExecutionResult.model_validate(resp.json())
        finally:
            if owns:
                await client.aclose()
