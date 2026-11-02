"""Independent BMX reporting scheduler.

This module deliberately has no playback-control dependency.  Transport
failures can degrade ReportingHealth, but can never stop, reselect or reboot a
radio through this scheduler.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit


REPORT_QUEUE_CAPACITY = 20
REPORT_MAX_RETRIES = 5
REPORT_MAX_ATTEMPTS = 1 + REPORT_MAX_RETRIES
DEFAULT_RETRY_BACKOFF_SECONDS = (5, 15, 60, 300, 900)


class ReportingStatus(StrEnum):
    IDLE = "IDLE"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SUCCESS = "SUCCESS"
    RETRY_WAIT = "RETRY_WAIT"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    RECOVERED = "RECOVERED"


class ReportingQueueFull(RuntimeError):
    pass


class ReportingCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportPayload:
    timeStamp: str
    eventType: str
    reason: str = ""
    timeIntoTrack: int = 0
    playbackDelay: int = 0
    absolutePlayPoint: str = ""
    reasonSubCode: str = ""

    def as_dict(self) -> dict[str, Any]:
        # These are the seven confirmed BMX.Report fields.  Do not add local
        # device/account/session identity to the wire payload.
        return asdict(self)


@dataclass
class ReportingQueueItem:
    item_id: str
    payload: ReportPayload
    attempts: int = 0
    due_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def retry_count(self) -> int:
        return max(0, self.attempts - 1)


@dataclass
class ReportingSession:
    key: str
    report_url: str | None = None
    status: ReportingStatus = ReportingStatus.IDLE
    queue: list[ReportingQueueItem] = field(default_factory=list)
    next_due_at: datetime | None = None
    last_http_status: int | None = None
    last_success_at: datetime | None = None
    last_failure: str | None = None
    semantic_persistent_error: bool = False
    generation: int = 0

    @property
    def queue_depth(self) -> int:
        return len(self.queue)

    @property
    def retry_count(self) -> int:
        return self.queue[0].retry_count if self.queue else 0


@dataclass(frozen=True)
class ReportingResult:
    status: ReportingStatus
    queue_depth: int
    retry_count: int
    next_due_at: datetime | None
    last_http_status: int | None
    embedded_now_playing: Mapping[str, Any] | None = None
    playback_action: str = "NONE"


class ReportResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


ReportPost = Callable[[str, Mapping[str, Any]], Awaitable[ReportResponse]]
Sleep = Callable[[float], Awaitable[None]]
ReportingResultHandler = Callable[[str, ReportingResult], Awaitable[None]]


class ReportingStore(Protocol):
    async def save(self, session: ReportingSession) -> None: ...


class NullReportingStore:
    async def save(self, session: ReportingSession) -> None:
        del session


def _aware(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _report_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return text


def redact_report_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def _uint32_seconds(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > (1 << 32) - 1:
        return None
    return parsed


def _response_json(response: ReportResponse) -> Mapping[str, Any]:
    try:
        value = response.json()
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}


def reporting_link(payload: Mapping[str, Any]) -> str | None:
    """Return the validated dynamic ``bmx_reporting`` link, if present."""

    links = payload.get("_links")
    if not isinstance(links, Mapping):
        return None
    link = links.get("bmx_reporting")
    if isinstance(link, Mapping):
        return _report_url(link.get("href"))
    return _report_url(link)


def embedded_now_playing(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return an embedded BMX.NowPlaying response without altering it."""

    embedded = payload.get("_embedded")
    if not isinstance(embedded, Mapping):
        return None
    value = embedded.get("bmx_nowplaying") or embedded.get("nowPlaying")
    return value if isinstance(value, Mapping) else None


class ReportingScheduler:
    """Single-flight, generation-safe, per-session reporting scheduler."""

    def __init__(
        self,
        post: ReportPost,
        *,
        store: ReportingStore | None = None,
        backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
        sleep: Sleep = asyncio.sleep,
        result_handler: ReportingResultHandler | None = None,
    ) -> None:
        if len(backoff_seconds) < REPORT_MAX_RETRIES:
            raise ValueError("reporting backoff must cover five retries")
        self._post = post
        self._store = store or NullReportingStore()
        self._backoff = tuple(max(0, int(value)) for value in backoff_seconds)
        self._sleep = sleep
        self._result_handler = result_handler
        self._sessions: dict[str, ReportingSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    def session(self, key: str) -> ReportingSession:
        return self._sessions.setdefault(key, ReportingSession(key=key))

    async def restore(self, session: ReportingSession) -> None:
        """Restore persisted state without firing a global catch-up loop."""

        self._sessions[session.key] = session
        if session.queue and session.status == ReportingStatus.IDLE:
            session.status = ReportingStatus.QUEUED
        await self._store.save(session)

    async def enqueue(
        self,
        key: str,
        payload: ReportPayload,
        *,
        report_url: str | None = None,
        due_at: datetime | None = None,
        item_id: str | None = None,
    ) -> ReportingSession:
        session = self.session(key)
        if len(session.queue) >= REPORT_QUEUE_CAPACITY:
            # Firmware overflow eviction is unresolved.  Failing closed keeps
            # every retained event observable rather than inventing an oldest
            # or newest eviction policy.
            session.status = ReportingStatus.DEGRADED
            session.last_failure = "REPORT_QUEUE_FULL"
            await self._store.save(session)
            raise ReportingQueueFull("reporting queue capacity 20 reached")
        candidate_url = _report_url(report_url)
        if report_url is not None and candidate_url is None:
            raise ValueError("report_url must be an absolute HTTP(S) URL")
        if candidate_url:
            session.report_url = candidate_url
        created = _aware()
        session.queue.append(
            ReportingQueueItem(
                item_id=item_id or f"{key}:{int(created.timestamp() * 1_000_000)}:{len(session.queue)}",
                payload=payload,
                due_at=_aware(due_at or created),
                created_at=created,
            )
        )
        session.status = ReportingStatus.QUEUED
        session.next_due_at = session.queue[0].due_at
        await self._store.save(session)
        return session

    async def update_report_url(self, key: str, report_url: str) -> ReportingSession:
        """Attach a freshly observed dynamic link without adding an event."""

        candidate = _report_url(report_url)
        if candidate is None:
            raise ValueError("report_url must be an absolute HTTP(S) URL without credentials")
        session = self.session(key)
        session.report_url = candidate
        if session.last_failure == "REPORT_URL_REFRESH_REQUIRED":
            session.last_failure = None
            session.status = ReportingStatus.QUEUED if session.queue else ReportingStatus.IDLE
        await self._store.save(session)
        return session

    async def process_due(
        self, key: str, *, now: datetime | None = None
    ) -> ReportingResult:
        session = self.session(key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            observed = _aware(now)
            if not session.queue:
                # ``nextReportIn`` persists an absolute TimedReport due time,
                # not a polling interval. Materialize its wire payload only
                # when due so the timestamp reflects the actual attempt.
                if session.next_due_at is None:
                    session.status = ReportingStatus.IDLE
                    await self._store.save(session)
                    return self._result(session)
                if _aware(session.next_due_at) > observed:
                    session.status = ReportingStatus.IDLE
                    await self._store.save(session)
                    return self._result(session)
                session.queue.append(
                    ReportingQueueItem(
                        item_id=f"{key}:timed:{int(observed.timestamp() * 1_000_000)}",
                        payload=ReportPayload(
                            timeStamp=observed.isoformat(),
                            eventType="timed",
                        ),
                        due_at=observed,
                        created_at=observed,
                    )
                )
                session.status = ReportingStatus.QUEUED
            item = session.queue[0]
            if item.due_at and _aware(item.due_at) > observed:
                session.status = ReportingStatus.QUEUED if item.attempts == 0 else ReportingStatus.RETRY_WAIT
                session.next_due_at = _aware(item.due_at)
                await self._store.save(session)
                return self._result(session)
            if not session.report_url:
                session.status = ReportingStatus.FAILED
                session.last_failure = "REPORT_URL_MISSING"
                await self._store.save(session)
                return self._result(session)

            generation = session.generation
            previous = session.status
            session.status = ReportingStatus.SENDING
            item.attempts += 1
            await self._store.save(session)
            try:
                response = await self._post(session.report_url, item.payload.as_dict())
                if session.generation != generation:
                    raise ReportingCancelled("reporting generation changed")
                session.last_http_status = int(response.status_code)
                if response.status_code == 200:
                    body = _response_json(response)
                    replacement = reporting_link(body)
                    if replacement:
                        session.report_url = replacement
                    session.queue.pop(0)
                    session.last_success_at = observed
                    session.last_failure = None
                    session.semantic_persistent_error = False
                    recovered = previous in {
                        ReportingStatus.RETRY_WAIT,
                        ReportingStatus.DEGRADED,
                        ReportingStatus.FAILED,
                    } or item.attempts > 1
                    session.status = ReportingStatus.RECOVERED if recovered else ReportingStatus.SUCCESS
                    hint = _uint32_seconds(body.get("nextReportIn"))
                    session.next_due_at = observed + timedelta(seconds=hint) if hint else None
                    if session.queue:
                        session.queue[0].due_at = session.queue[0].due_at or observed
                        session.next_due_at = session.queue[0].due_at
                    await self._store.save(session)
                    return await self._result_and_notify(
                        session, embedded=embedded_now_playing(body)
                    )
                failure = f"HTTP_{response.status_code}"
            except ReportingCancelled:
                raise
            except Exception as exc:
                failure = f"TRANSPORT_{type(exc).__name__}"
                session.last_http_status = None

            item.last_error = failure
            session.last_failure = failure
            if item.attempts >= REPORT_MAX_ATTEMPTS:
                session.queue.pop(0)
                session.status = ReportingStatus.FAILED
                session.next_due_at = session.queue[0].due_at if session.queue else None
            else:
                # attempts=1 is the initial send and schedules retry #1 using
                # the first BASSWIESN backoff slot.
                backoff = self._backoff[item.attempts - 1]
                item.due_at = observed + timedelta(seconds=backoff)
                session.status = ReportingStatus.RETRY_WAIT
                session.next_due_at = item.due_at
            await self._store.save(session)
            return self._result(session)

    async def _result_and_notify(
        self,
        session: ReportingSession,
        *,
        embedded: Mapping[str, Any] | None = None,
    ) -> ReportingResult:
        """Publish a response observation without coupling its failure back.

        Embedded metadata is a separate health contract.  A metadata consumer
        failure therefore cannot turn an accepted report into a reporting
        retry or playback action.
        """

        result = self._result(session, embedded=embedded)
        if self._result_handler is not None:
            try:
                await self._result_handler(session.key, result)
            except Exception:
                # The owning runtime records metadata failures separately.
                # Reporting success/failure semantics must remain unchanged.
                pass
        return result

    async def mark_semantic_error(self, key: str, *, persistent: bool) -> ReportingSession:
        session = self.session(key)
        session.semantic_persistent_error = bool(persistent)
        session.status = ReportingStatus.DEGRADED
        session.last_failure = "PERSISTENT_BMX_ERROR" if persistent else "TEMPORARY_BMX_ERROR"
        await self._store.save(session)
        return session

    def schedule_due(self, key: str, *, now: Callable[[], datetime] | None = None) -> None:
        """Schedule only this session's persisted absolute due time.

        A successful response or retry can replace ``next_due_at``.  The
        per-session task therefore arms exactly one successor after processing
        the response.  This is intentionally not a global polling loop.
        """

        if self._closed:
            return
        self.cancel_task(key)
        self._arm_due(key, generation=self.session(key).generation, now=now)

    def _arm_due(
        self,
        key: str,
        *,
        generation: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        session = self.session(key)
        due = session.next_due_at
        # Restored sessions intentionally have no operational URL until a
        # freshly observed dynamic link is attached.  Do not spin on a past
        # due time while waiting for that link.
        if self._closed or due is None or not session.report_url:
            return
        clock = now or (lambda: datetime.now(UTC))

        async def runner() -> None:
            current_task = asyncio.current_task()
            try:
                delay = max(0.0, (_aware(due) - _aware(clock())).total_seconds())
                await self._sleep(delay)
                if self.session(key).generation != generation:
                    return
                await self.process_due(key, now=_aware(clock()))
            finally:
                if self._tasks.get(key) is current_task:
                    self._tasks.pop(key, None)

            current = self.session(key)
            if (
                not self._closed
                and current.generation == generation
                and current.next_due_at is not None
            ):
                self._arm_due(key, generation=generation, now=clock)

        task = asyncio.create_task(runner(), name=f"reporting:{key}")
        self._tasks[key] = task

    def cancel_task(self, key: str) -> None:
        session = self.session(key)
        session.generation += 1
        task = self._tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    async def cancel(self, key: str, *, clear_queue: bool = False) -> None:
        self.cancel_task(key)
        session = self.session(key)
        if clear_queue:
            session.queue.clear()
        session.status = ReportingStatus.IDLE
        session.next_due_at = None
        await self._store.save(session)

    async def shutdown(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for key in list(self._tasks):
            self.cancel_task(key)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _result(
        session: ReportingSession,
        *,
        embedded: Mapping[str, Any] | None = None,
    ) -> ReportingResult:
        return ReportingResult(
            status=session.status,
            queue_depth=session.queue_depth,
            retry_count=session.retry_count,
            next_due_at=session.next_due_at,
            last_http_status=session.last_http_status,
            embedded_now_playing=embedded,
            playback_action="NONE",
        )
