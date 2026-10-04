# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""OpenGammon sync: a player's finished matches, fetched from here.

The plan is `docs/OpenGammonSync.md` in the GammonView repo, and the account's
side is `accounts/gvaccounts/ogsync.py` there. This is the part that talks to
opengammon.com. What the code below rests on:

**The helper asks, because it has the player's own address.** opengammon.com
limits requests per address, and GammonView's server has one address for
everybody. So the requests come from here, and the site only remembers what was
found. Nothing in this module needs an OpenGammon login.

**The helper fetches; the browser converts and saves.** A fetched match goes to
the account's inbox as the `.mat` plus either OpenGammon's analysis record or
the `.gvab` this computer made from the `.mat`. Turning that into a saved match
is the browser's existing import path.

**The account holds the cursors, and `og_seen` holds the memory.** The two
cursors say where to *look*: `settled_before` for new matches, every
`CHECK_INTERVAL`, and `backfill_before` for history, a page at a time. Neither
moves past a match that has not been taken or skipped, so a helper that stops
at any point -- quit, crashed, laptop shut -- starts again where it was, and
the worst a restart costs is a repeated list request.

**Being a good guest.** OpenGammon's analysis route allows 200 a day per
address, and the player's own browsing spends the same allowance, so the helper
takes at most `DAILY_ANALYSES`. Every request is spaced `REQUEST_GAP` apart, and
a 429 stops all of them until its `retry_after` has passed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from . import USER_AGENT
from .client import RelayError, SyncChanged, Unauthorized, WorkerClient
from .config import config_dir

#: OpenGammon's API. `/app/`, not `/api/app/`: the latter is the single-page
#: app's catch-all and answers every path with HTML.
OG_API = "https://opengammon.com/app"

DAY = 24 * 60 * 60

#: How often new matches are looked for. One list request, normally answering
#: "nothing new": 48 a day.
CHECK_INTERVAL = 30 * 60

#: The longest one `og-wait` is held: the site's own cap, under nginx's 30 s.
WAIT_SECONDS = 25

#: How often the setting is re-read from the site, through `hello`. Shorter
#: than a check so that a player who has just turned sync on sees it start
#: within minutes, and it costs a request to our own server, not OpenGammon's.
HELLO_INTERVAL = 5 * 60

#: The least time between two requests to opengammon.com. Their site-wide
#: limit is 120 per two minutes per address; this keeps the helper to half of
#: that at the very most, leaving the rest for the player's own browsing.
REQUEST_GAP = 2.0

#: Analyses fetched per day. OpenGammon allows 200 a day per address on that
#: route, and the player's own browsing from the same address counts too.
DAILY_ANALYSES = 150

#: How far behind "now" the new-matches cursor stays when nothing is waiting:
#: longer than any match lasts, because the list is sorted by when a match
#: *started* and holds only finished ones.
SETTLE_MARGIN = 2 * DAY

#: How long a recent match may stay unanalysed before it is given up on. A
#: match OpenGammon has not analysed a day after it started is not going to be.
GIVE_UP_AFTER = DAY

#: History pauses while the inbox holds more than this, so a computer left on
#: for a fortnight does not pile a player's whole history into the account.
INBOX_PAUSE = 50

#: List page sizes. OpenGammon caps `count` at 100. History analysed on this
#: computer takes a few minutes a match, and the cursor moves only when a whole
#: page is done, so its pages are small: a restart repeats at most a few.
PAGE = 100
LOCAL_PAGE = 10

#: Pause between two history pages, so history never runs flat out.
HISTORY_GAP = 5.0

#: Backoff after a failed round, as the work loop does it.
RETRY_MIN = 30.0
RETRY_MAX = 30 * 60.0


def window_wait(start: int | None, end: int | None, at: float) -> float:
    """Seconds from `at` until local analysis may run: 0 inside the window, or
    with no window.

    `start` and `end` are minutes after midnight on this computer's clock, which
    is the point: "overnight" means the player's night wherever the computer
    is. A window may cross midnight (22:00 to 07:00).
    """
    if start is None or end is None:
        return 0.0
    t = time.localtime(at)
    now = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    begin, stop = start * 60, end * 60
    inside = begin <= now < stop if begin < stop else (now >= begin or now < stop)
    return 0.0 if inside else float((begin - now) % DAY)


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] opengammon: {message}", flush=True)


# --- opengammon.com ------------------------------------------------------------


class OgError(Exception):
    """opengammon.com refused, or answered with something unreadable."""


class OgRateLimited(OgError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited for {retry_after:.0f}s")
        self.retry_after = retry_after


class Allowance:
    """The day's count of analysis requests, and any 429 still in force.

    Kept in a file beside the config, so a restart does not reset it. The day
    is UTC, which is near enough to OpenGammon's own window for a margin of 50.
    """

    def __init__(self, path=None, now: Callable[[], float] = time.time):
        self.path = path or (config_dir() / "opengammon.json")
        self.now = now
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data))
        except OSError as e:  # pragma: no cover - a full disk is not worth stopping for
            _log(f"could not save the day's count: {e}")

    def _today(self) -> dict:
        day = datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y-%m-%d")
        if self._data.get("day") != day:
            self._data = {"day": day, "analyses": 0, "synced": 0,
                          "blocked_until": self._data.get("blocked_until", 0)}
        return self._data

    def analyses_left(self) -> int:
        with self._lock:
            return max(0, DAILY_ANALYSES - int(self._today().get("analyses", 0)))

    def spend_analysis(self) -> None:
        with self._lock:
            today = self._today()
            today["analyses"] = int(today.get("analyses", 0)) + 1
            self._save()

    def note_synced(self) -> None:
        with self._lock:
            today = self._today()
            today["synced"] = int(today.get("synced", 0)) + 1
            self._save()

    def synced_today(self) -> int:
        with self._lock:
            return int(self._today().get("synced", 0))

    def block(self, seconds: float) -> None:
        with self._lock:
            self._today()["blocked_until"] = self.now() + max(1.0, seconds)
            self._save()

    def blocked_for(self) -> float:
        with self._lock:
            return max(0.0, float(self._data.get("blocked_until", 0)) - self.now())


@dataclass
class Entry:
    """The few fields of a list entry the sync uses. Nothing else is kept --
    an entry embeds both players' whole user records."""

    id: str
    create_time: int
    analysis_ply: int


class OpenGammon:
    """The three requests the sync makes, spaced and counted."""

    def __init__(self, allowance: Allowance, client: httpx.Client | None = None,
                 base: str = OG_API, sleep: Callable[[float], None] = time.sleep):
        self.allowance = allowance
        self.base = base
        self.sleep = sleep
        self._client = client or httpx.Client(
            timeout=30.0, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        self._last = 0.0

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        blocked = self.allowance.blocked_for()
        if blocked:
            raise OgRateLimited(blocked)
        wait = self._last + REQUEST_GAP - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()
        try:
            r = self._client.get(f"{self.base}{path}", params=params)
        except httpx.RequestError as e:
            raise OgError(f"could not reach opengammon.com ({type(e).__name__})") from e
        if r.status_code == 429:
            try:
                retry = float(r.json().get("retry_after") or 0)
            except Exception:  # noqa: BLE001 - any unreadable 429 waits the default
                retry = 0.0
            retry = retry or float(r.headers.get("retry-after") or 0) or 15 * 60
            self.allowance.block(retry)
            raise OgRateLimited(retry)
        if r.status_code >= 400:
            raise OgError(f"opengammon.com answered HTTP {r.status_code} for {path}")
        try:
            body = r.json()
        except ValueError as e:
            raise OgError(f"opengammon.com sent something unreadable for {path}") from e
        if not isinstance(body, dict) or body.get("status") == "error":
            message = body.get("message") if isinstance(body, dict) else None
            raise OgError(f"opengammon.com refused {path}: {message or 'no reason given'}")
        return body

    def matches(self, user_id: str, start: int, end: int, count: int) -> tuple[list[dict], int]:
        """One page of finished player-vs-player matches started in [start, end],
        newest first, and how many the whole range holds."""
        body = self._get(f"/user/{user_id}/matches/", {
            "count": count, "date_range": f"{int(start)},{int(end)}",
            "match_type": "pvp", "status": "finished",
        })
        raw = body.get("matches")
        raw = raw if isinstance(raw, list) else []
        total = body.get("total")
        return raw, total if isinstance(total, int) else len(raw)

    def mat(self, match_id: str) -> str:
        body = self._get(f"/match/{match_id}/export/", {"format": "mat"})
        mat = body.get("match")
        if not isinstance(mat, str) or not mat.strip():
            raise OgError(f"match {match_id} came back without a match file")
        return mat

    def analysis(self, match_id: str) -> dict | None:
        """The analysis record, or None if it is not finished.

        Counted against the day's allowance whatever the answer, because
        OpenGammon counts it. **Asking for an unanalysed match queues a job on
        their servers**, so this is only ever called for a list entry whose
        `analysis_ply` says the analysis is there.
        """
        self.allowance.spend_analysis()
        body = self._get(f"/match/{match_id}/analysis/")
        record = body.get("analysis")
        if not isinstance(record, dict) or record.get("status") != "analysed":
            return None
        if not isinstance(record.get("analysis"), dict):
            return None
        return record


def entries(raw: list[dict]) -> list[Entry]:
    """The list entries worth considering: finished player-vs-player matches.

    The list is asked for `match_type=pvp` already; bot and daily matches are
    dropped here as well, in case that default ever changes (decision 4).
    """
    out = []
    for m in raw:
        if not isinstance(m, dict) or m.get("match_type") != "pvp":
            continue
        if m.get("status", "finished") != "finished":
            continue
        match_id, created = m.get("match_id"), m.get("create_time")
        if not isinstance(match_id, str) or not isinstance(created, (int, float)):
            continue
        ply = m.get("analysis_ply")
        out.append(Entry(match_id, int(created), int(ply) if isinstance(ply, (int, float)) else 0))
    return out


#: A move line in a JellyFish `.mat`: a move number and then a roll. A forfeit
#: on OpenGammon is a match with none -- its one game is a winner and nothing
#: else -- and there is nothing in it to analyse or to look at.
_ROLL = re.compile(r"^\s*\d+\)\s+\d\d:", re.MULTILINE)


def has_play(mat: str) -> bool:
    """Whether a `.mat` holds at least one roll."""
    return bool(_ROLL.search(mat))


def _oldest(raw: list[dict]) -> int | None:
    times = [int(m["create_time"]) for m in raw
             if isinstance(m, dict) and isinstance(m.get("create_time"), (int, float))]
    return min(times) if times else None


# --- handing local analysis to the work loop -------------------------------------


class LocalWork:
    """A synced `.mat` waiting for the work loop to analyse it.

    The analysis runs on the work loop's thread, not this one, and only when
    the site has nothing queued: a match the player uploads by hand always goes
    first, so a long history never makes the helper look stuck. One at a time,
    because the sync thread waits for each before fetching the next -- which is
    also the back-pressure the plan asks for.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: dict | None = None
        self._closed = False

    def pending(self) -> bool:
        with self._cond:
            return self._item is not None and "taken" not in self._item

    def submit(self, mat: str, preset: str | None, label: str) -> bytes:
        """Wait for the work loop to analyse `mat`. Raises on failure, or
        `InterruptedError` if the helper is quitting."""
        item = {"mat": mat, "preset": preset, "label": label}
        with self._cond:
            if self._closed:
                raise InterruptedError("the helper is quitting")
            self._item = item
            self._cond.notify_all()
            self._cond.wait_for(lambda: "result" in item or "error" in item or self._closed)
            self._item = None
        if "result" in item:
            return item["result"]
        if "error" in item:
            raise item["error"]
        raise InterruptedError("the helper is quitting")

    def take(self) -> dict | None:
        with self._cond:
            if self._item is None or "taken" in self._item:
                return None
            self._item["taken"] = True
            return self._item

    def finish(self, item: dict, result: bytes | None = None,
               error: BaseException | None = None) -> None:
        with self._cond:
            if error is not None:
                item["error"] = error
            else:
                item["result"] = result
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


# --- the sync ----------------------------------------------------------------------


class Exhausted(Exception):
    """The day's analysis allowance is spent; stop and carry on tomorrow."""


class Syncer:
    """The sync thread's logic, one round at a time.

    `hello` returns the site's `hello` answer; the setting is its `og_sync`.
    `presets` is what this computer's engine accepts, for checking the
    player's sync preset against.
    """

    def __init__(self, client: WorkerClient, hello: Callable[[], dict], *,
                 presets: Callable[[], list[str]] = lambda: [],
                 og: OpenGammon | None = None, allowance: Allowance | None = None,
                 work: LocalWork | None = None, control=None,
                 now: Callable[[], float] = time.time):
        self.client = client
        self.hello = hello
        self.presets = presets
        self.allowance = allowance or Allowance(now=now)
        self.og = og or OpenGammon(self.allowance)
        self.work = work or LocalWork()
        self.control = control
        self.now = now
        self.state: dict | None = None
        #: How many matches history has still to look at, from the last page.
        self.history_left: int | None = None
        #: The player pressed "Check now", and the check has not run yet.
        self.check_asked = False

    # -- the setting --

    def refresh(self) -> dict | None:
        state = self.hello().get("og_sync")
        self.state = state if isinstance(state, dict) and state.get("sync_id") else None
        if self.state is None:
            self.history_left = None
        self._report()
        return self.state

    def _sync_id(self) -> str:
        assert self.state is not None
        return self.state["sync_id"]

    def _local(self) -> bool:
        return bool(self.state) and self.state.get("analysis") == "local"

    def window_wait(self) -> float:
        """Seconds until this computer may analyse for sync; 0 when it may now.

        Only local analysis has a window: fetching OpenGammon's own analysis
        costs this computer nothing. Outside it nothing is fetched either -- a
        match taken now would only sit waiting for the engine.
        """
        if not self._local():
            return 0.0
        assert self.state is not None
        return window_wait(self.state.get("window_start"), self.state.get("window_end"),
                           self.now())

    def preset(self) -> str | None:
        """The player's sync preset, if this computer's engine has it.

        A preset the installed engine no longer has falls back to the engine's
        default rather than leaving the match unanalysed. None means that
        default.
        """
        wanted = (self.state or {}).get("preset")
        if wanted and wanted in self.presets():
            return wanted
        if wanted:
            _log(f"preset {wanted!r} is not available here; using the engine's default")
        return None

    def _moved(self, state: dict | None) -> None:
        if state is not None:
            self.state = state

    def _report(self) -> None:
        """The menu's line about sync, or None when there is nothing to say."""
        if self.control is None:
            return
        line = None
        if self.state is not None:
            synced = self.allowance.synced_today()
            line = f"OpenGammon: {synced} match{'es' if synced != 1 else ''} synced today"
            if self.state.get("backfill_before") is not None and self.history_left:
                line += f", {self.history_left} older to go"
            if self.window_wait():
                start = self.state["window_start"]
                line += f"; analysing from {start // 60:02d}:{start % 60:02d}"
        self.control.og_line = line

    # -- taking one match --

    def _take(self, entry: Entry, *, history: bool) -> str:
        """Fetch one match into the inbox. 'taken', 'skipped' or 'later'.

        'later' is a recent match whose analysis is not ready; the new-matches
        cursor stays behind it until it is, or until `GIVE_UP_AFTER`.
        """
        assert self.state is not None
        old = history or entry.create_time < self.now() - GIVE_UP_AFTER
        sync_id = self._sync_id()

        if not self._local():
            if entry.analysis_ply <= 0:
                # Never asked for: asking would queue an analysis on their side.
                if not old:
                    return "later"
                self.client.og_skip(sync_id, entry.id, entry.create_time)
                return "skipped"
            if self.allowance.analyses_left() <= 0:
                raise Exhausted()
            # The `.mat` first: its route has only the site-wide limit, and a
            # forfeit found here costs none of the day's analyses.
            mat = self.og.mat(entry.id)
            if not has_play(mat):
                self.client.og_skip(sync_id, entry.id, entry.create_time)
                return "skipped"
            record = self.og.analysis(entry.id)
            if record is None:
                if not old:
                    return "later"
                self.client.og_skip(sync_id, entry.id, entry.create_time)
                return "skipped"
            depth = self.client.og_take(sync_id, entry.id, entry.create_time, mat,
                                        analysis=json.dumps(record, separators=(",", ":")))
        else:
            mat = self.og.mat(entry.id)
            if not has_play(mat):
                self.client.og_skip(sync_id, entry.id, entry.create_time)
                return "skipped"
            try:
                gvab = self.work.submit(mat, self.preset(), entry.id)
            except InterruptedError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad match must not stop the sync
                _log(f"could not analyse {entry.id}: {type(e).__name__}: {e}")
                self.client.og_skip(sync_id, entry.id, entry.create_time)
                return "skipped"
            depth = self.client.og_take(sync_id, entry.id, entry.create_time, mat, gvab=gvab)

        self.state["inbox_depth"] = depth
        self.allowance.note_synced()
        self._report()
        _log(f"took {entry.id}")
        return "taken"

    def _unseen(self, found: list[Entry]) -> list[Entry]:
        ids: list[str] = []
        for i in range(0, len(found), PAGE):
            ids += self.client.og_unseen(self._sync_id(), [e.id for e in found[i:i + PAGE]])
        keep = set(ids)
        return [e for e in found if e.id in keep]

    # -- new matches --

    def check(self, *, asked: bool = False) -> None:
        """Take every new match since `settled_before`, then move it up.

        It moves to the start of the oldest match still waiting for its
        analysis, or to `now - SETTLE_MARGIN` with none waiting -- never past a
        match that has not been taken.
        """
        assert self.state is not None
        # A check the player asked for runs outside the window too: pressing
        # the button is saying the computer may work now.
        if self.window_wait() and not asked:
            return
        now = int(self.now())
        start = int(self.state["settled_before"])
        end = now
        found: dict[str, Entry] = {}
        while True:
            raw, _ = self.og.matches(self.state["og_user_id"], start, end, PAGE)
            for e in entries(raw):
                found[e.id] = e
            oldest = _oldest(raw)
            if len(raw) < PAGE or oldest is None:
                break
            end = oldest if oldest < end else end - 1

        waiting: list[int] = []
        new = sorted(self._unseen(list(found.values())), key=lambda e: e.create_time)
        for n, e in enumerate(new):
            try:
                outcome = self._take(e, history=False)
            except (Exhausted, OgRateLimited):
                waiting += [x.create_time for x in new[n:]]
                break
            except OgError as err:
                _log(f"could not fetch {e.id}: {err}")
                outcome = "later"
            if outcome == "later":
                waiting.append(e.create_time)

        settled = min(waiting) if waiting else now - SETTLE_MARGIN
        if settled > start:
            self._moved(self.client.og_cursor(self._sync_id(), settled_before=settled))

    # -- history --

    def history_step(self) -> bool:
        """One page of history, oldest-reaching. False when there is nothing to
        do right now: history is done, the inbox is full, or the day's
        allowance is spent."""
        assert self.state is not None
        before = self.state.get("backfill_before")
        if before is None or self.window_wait():
            return False
        if int(self.state.get("inbox_depth") or 0) > INBOX_PAUSE:
            return False
        if not self._local() and self.allowance.analyses_left() <= 0:
            return False
        floor = int(self.state.get("backfill_floor") or 0)
        count = LOCAL_PAGE if self._local() else PAGE
        raw, total = self.og.matches(self.state["og_user_id"], floor, int(before), count)
        self.history_left = total
        self._report()

        for e in self._unseen(entries(raw)):
            try:
                self._take(e, history=True)
            except Exhausted:
                return False
            except OgRateLimited:
                raise
            except OgError as err:
                _log(f"could not fetch {e.id}: {err}; leaving it")
                self.client.og_skip(self._sync_id(), e.id, e.create_time)

        oldest = _oldest(raw)
        if len(raw) < count or oldest is None:
            self._moved(self.client.og_cursor(self._sync_id(), history_done=True,
                                              backfill_floor=floor))
            self.history_left = 0
            _log("history is done")
        else:
            # The bound is inclusive, so the next page repeats the oldest match
            # here and `og_seen` filters it. A page all started in one second
            # would never move; stepping back a second is the way out.
            if oldest >= int(before):
                oldest = int(before) - 1
            self._moved(self.client.og_cursor(self._sync_id(), backfill_before=oldest))
        self._report()
        return True

    # -- the thread --

    def _sleep(self, seconds: float) -> None:
        if self.control is not None:
            self.control.sleep(seconds)
        else:  # pragma: no cover - the tests drive rounds directly
            time.sleep(seconds)

    def _idle(self, seconds: float) -> None:
        """Wait `seconds`, or less if the player presses "Check now".

        Spent holding `og-wait` open rather than asleep, which is what lets the
        button answer in seconds. A site without the route is slept on.
        """
        deadline = time.monotonic() + seconds
        while not self._stopping():
            left = deadline - time.monotonic()
            if left <= 0:
                return
            asked = self.client.og_wait(int(min(left, WAIT_SECONDS)) or 1)
            if asked is None:
                self._sleep(left)
                return
            if asked:
                self.check_asked = True
                return

    def _stopping(self) -> bool:
        return self.control is not None and self.control.stopping

    def run(self) -> None:
        """Check, walk history, sleep; until the helper quits.

        Runs while paused too: pausing stops analysis on this computer, and
        fetching OpenGammon's analysis is not that. A player who chose local
        analysis finds the sync waiting on the work loop instead, which pause
        does stop.
        """
        next_check = 0.0
        next_hello = 0.0
        delay = RETRY_MIN
        try:
            while not self._stopping():
                try:
                    blocked = self.allowance.blocked_for()
                    if blocked:
                        self._sleep(min(blocked, HELLO_INTERVAL))
                        continue
                    if time.monotonic() >= next_hello or self.state is None:
                        self.refresh()
                        next_hello = time.monotonic() + HELLO_INTERVAL
                    if self.state is None:
                        self._sleep(HELLO_INTERVAL)
                        continue
                    if self.check_asked:
                        _log("checking now, as asked")
                        self.refresh()
                        if self.state is not None:
                            self.check(asked=True)
                            self._moved(self.client.og_cursor(self._sync_id(), checked=True))
                        self.check_asked = False
                        next_check = time.monotonic() + CHECK_INTERVAL
                        next_hello = time.monotonic() + HELLO_INTERVAL
                        continue
                    outside = self.window_wait()
                    if outside:
                        # Look for new matches the moment the window opens,
                        # rather than up to a check interval later.
                        next_check = 0.0
                        self._idle(max(1.0, min(outside, next_hello - time.monotonic())))
                        continue
                    if time.monotonic() >= next_check:
                        self.check()
                        next_check = time.monotonic() + CHECK_INTERVAL
                        delay = RETRY_MIN
                        continue
                    if self.history_step():
                        delay = RETRY_MIN
                        self._idle(HISTORY_GAP)
                        continue
                    self._idle(max(1.0, min(next_check, next_hello) - time.monotonic()))
                except SyncChanged:
                    next_hello = 0.0
                except Unauthorized:
                    return
                except InterruptedError:
                    return
                except OgRateLimited as e:
                    _log(f"opengammon.com asked us to wait {e.retry_after:.0f}s")
                except (RelayError, OgError) as e:
                    _log(f"{e} -- trying again in {delay:.0f}s")
                    self._sleep(delay)
                    delay = min(RETRY_MAX, delay * 2)
                except Exception as e:  # noqa: BLE001 - the sync must never take the helper down
                    _log(f"{type(e).__name__}: {e} -- trying again in {delay:.0f}s")
                    self._sleep(delay)
                    delay = min(RETRY_MAX, delay * 2)
        finally:
            self.og.close()
