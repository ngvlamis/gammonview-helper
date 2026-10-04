# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""OpenGammon sync, against a fake OpenGammon and a fake account.

What these hold the sync to:

  * **No cursor moves past a match that was not taken or skipped.** That is
    what makes a restart at any point safe.
  * **Nothing is asked of OpenGammon's analysis route that would queue a job**:
    an entry with `analysis_ply` 0 is never fetched.
  * **The day's allowance is kept**, and a page cut short by it is repeated
    tomorrow rather than skipped.
  * **Bot matches are dropped** even if the list sends them.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from gvhelper import ogsync
from gvhelper.ogsync import Allowance, LocalWork, OpenGammon, Syncer, window_wait

NOW = 1_800_000_000
DAY = ogsync.DAY


def m(match_id, created, ply=2, match_type="pvp"):
    return {"match_id": match_id, "create_time": created, "analysis_ply": ply,
            "match_type": match_type, "status": "finished"}


class FakeOg:
    """OpenGammon's list, newest first and inclusive at both ends, as probed."""

    def __init__(self, matches, allowance):
        self.all = sorted(matches, key=lambda x: -x["create_time"])
        self.allowance = allowance
        self.analysed: list[str] = []
        self.lists: list[tuple[int, int, int]] = []

    def matches(self, user_id, start, end, count):
        self.lists.append((start, end, count))
        hits = [x for x in self.all if start <= x["create_time"] <= end]
        return hits[:count], len(hits)

    def mat(self, match_id):
        if match_id.startswith("forfeit"):
            return " Game 1\n  1)\n   Wins 5 points and the match\n"
        return f"; match {match_id}\n  1) 31: 8/5 6/5      42: 8/4 6/4\n"

    def analysis(self, match_id):
        self.allowance.spend_analysis()
        self.analysed.append(match_id)
        entry = next(x for x in self.all if x["match_id"] == match_id)
        assert entry["analysis_ply"] > 0, "would have queued a job on OpenGammon"
        return {"status": "analysed", "analysis": {"games": []}, "analysed_time": 1.0}

    def close(self):
        pass


class FakeAccount:
    """The account's side: `og_seen`, the inbox, and one-way cursors."""

    def __init__(self, analysis="opengammon", floor=0, settled=NOW - DAY, before=NOW):
        self.state = {"sync_id": "s1", "og_user_id": "kit", "analysis": analysis,
                      "preset": "fast", "settled_before": settled,
                      "backfill_before": before, "backfill_floor": floor, "inbox_depth": 0}
        self.seen: dict[str, str] = {}
        self.inbox: dict[str, dict] = {}
        self.saved: dict[str, dict] = {}

    def drain(self, syncer):
        """The browser saving everything, and the helper's next hello hearing so."""
        self.saved.update(self.inbox)
        self.inbox.clear()
        self.state["inbox_depth"] = 0
        syncer.refresh()

    def taken(self):
        return {**self.saved, **self.inbox}

    def register(self):
        return {"account": "kit@example.com", "og_sync": dict(self.state)}

    def og_unseen(self, sync_id, ids):
        return [i for i in ids if i not in self.seen]

    def og_take(self, sync_id, match_id, create_time, mat, *, analysis=None, gvab=None):
        self.seen.setdefault(match_id, "taken")
        self.inbox[match_id] = {"mat": mat, "analysis": analysis, "gvab": gvab}
        self.state["inbox_depth"] = len(self.inbox)
        return len(self.inbox)

    def og_skip(self, sync_id, match_id, create_time):
        self.seen.setdefault(match_id, "no-analysis")

    def og_cursor(self, sync_id, settled_before=None, backfill_before=None,
                  history_done=False, backfill_floor=None):
        s = self.state
        if settled_before is not None and settled_before > s["settled_before"]:
            s["settled_before"] = settled_before
        if history_done and backfill_floor == s["backfill_floor"]:
            s["backfill_before"] = None
        elif backfill_before is not None and s["backfill_before"] is not None \
                and backfill_before < s["backfill_before"]:
            s["backfill_before"] = backfill_before
        return dict(s)


@pytest.fixture
def allowance(tmp_path):
    return Allowance(tmp_path / "og.json", now=lambda: NOW)


def syncer(account, og, allowance, work=None):
    s = Syncer(account, account.register, presets=lambda: ["fast", "world_class"],
               og=og, allowance=allowance, work=work, now=lambda: NOW)
    s.refresh()
    return s


# --- new matches ---------------------------------------------------------------


def test_new_matches_are_taken_and_the_cursor_settles_behind_them(allowance):
    account = FakeAccount()
    og = FakeOg([m("a", NOW - 3600), m("b", NOW - 600)], allowance)
    s = syncer(account, og, allowance)
    s.check()
    assert set(account.inbox) == {"a", "b"}
    assert json.loads(account.inbox["a"]["analysis"])["status"] == "analysed"
    # Nothing waiting, so the cursor goes to the margin -- not to the newest match.
    # The fake starts it at NOW - DAY, ahead of the margin, so it stays there.
    assert account.state["settled_before"] == NOW - DAY
    s.check()
    assert og.analysed == ["a", "b"], "a second check must not fetch anything again"


def test_a_recent_unanalysed_match_holds_the_cursor_and_is_never_asked_for(allowance):
    account = FakeAccount(settled=NOW - 3 * DAY)
    og = FakeOg([m("done", NOW - 2 * DAY), m("pending", NOW - 3600, ply=0),
                 m("after", NOW - 600)], allowance)
    s = syncer(account, og, allowance)
    s.check()
    assert set(account.inbox) == {"done", "after"}
    assert "pending" not in og.analysed
    assert account.state["settled_before"] == NOW - 3600
    assert "pending" not in account.seen


def test_an_unanalysed_match_is_given_up_on_after_a_day(allowance):
    account = FakeAccount(settled=NOW - 3 * DAY)
    og = FakeOg([m("stale", NOW - 2 * DAY, ply=0)], allowance)
    s = syncer(account, og, allowance)
    s.check()
    assert account.seen == {"stale": "no-analysis"}
    assert account.state["settled_before"] == NOW - ogsync.SETTLE_MARGIN


def test_bot_matches_are_dropped_even_if_listed(allowance):
    account = FakeAccount()
    og = FakeOg([m("bot", NOW - 600, match_type="bot")], allowance)
    s = syncer(account, og, allowance)
    s.check()
    assert account.inbox == {} and account.seen == {}


# --- history -----------------------------------------------------------------------


def test_history_walks_back_to_the_floor_and_finishes(allowance):
    matches = [m(f"h{i}", NOW - DAY - i * 60) for i in range(250)]
    account = FakeAccount(floor=0, before=NOW)
    og = FakeOg(matches, allowance)
    allowance_big = allowance
    ogsync_daily = ogsync.DAILY_ANALYSES
    try:
        ogsync.DAILY_ANALYSES = 1000
        s = syncer(account, og, allowance_big)
        steps = 0
        while s.history_step():
            account.drain(s)
            steps += 1
            assert steps < 10
    finally:
        ogsync.DAILY_ANALYSES = ogsync_daily
    assert len(account.taken()) == 250
    assert account.state["backfill_before"] is None


def test_the_daily_allowance_stops_history_and_tomorrow_resumes_it(tmp_path):
    clock = [NOW]
    allowance = Allowance(tmp_path / "og.json", now=lambda: clock[0])
    matches = [m(f"h{i}", NOW - DAY - i * 60) for i in range(200)]
    account = FakeAccount(floor=0, before=NOW)
    og = FakeOg(matches, allowance)
    s = Syncer(account, account.register, og=og, allowance=allowance, now=lambda: clock[0])
    s.refresh()
    while s.history_step():
        account.drain(s)
    assert len(account.taken()) == ogsync.DAILY_ANALYSES
    # The first page finished and moved the cursor; the second was cut short at
    # 50 and must not have moved it.
    assert account.state["backfill_before"] == NOW - DAY - 99 * 60

    clock[0] += DAY
    allowance._data = allowance._load()
    while s.history_step():
        account.drain(s)
    assert len(account.taken()) == 200
    assert account.state["backfill_before"] is None
    assert len(og.analysed) == 200, "nothing fetched twice"


def test_history_skips_what_opengammon_never_analysed(allowance):
    account = FakeAccount(floor=0, before=NOW)
    og = FakeOg([m("old", NOW - 400 * DAY, ply=0), m("ok", NOW - 300 * DAY)], allowance)
    s = syncer(account, og, allowance)
    s.history_step()
    assert account.seen == {"old": "no-analysis", "ok": "taken"}
    assert og.analysed == ["ok"]


def test_a_full_inbox_pauses_history(allowance):
    account = FakeAccount(before=NOW)
    account.state["inbox_depth"] = ogsync.INBOX_PAUSE + 1
    og = FakeOg([m("h", NOW - 300 * DAY)], allowance)
    s = syncer(account, og, allowance)
    assert s.history_step() is False
    assert og.lists == []


def test_a_page_all_started_in_one_second_still_moves(allowance):
    same = [m(f"x{i}", NOW - DAY) for i in range(ogsync.PAGE)]
    account = FakeAccount(before=NOW - DAY)
    og = FakeOg(same + [m("older", NOW - 2 * DAY)], allowance)
    try:
        saved, ogsync.DAILY_ANALYSES = ogsync.DAILY_ANALYSES, 1000
        s = syncer(account, og, allowance)
        s.history_step()
        assert account.state["backfill_before"] == NOW - DAY - 1
        account.drain(s)
        s.history_step()
        assert "older" in account.inbox
    finally:
        ogsync.DAILY_ANALYSES = saved


def test_a_forfeit_is_skipped_without_spending_an_analysis(allowance):
    account = FakeAccount(floor=0, before=NOW)
    og = FakeOg([m("forfeit1", NOW - 300 * DAY, ply=100)], allowance)
    s = syncer(account, og, allowance)
    s.history_step()
    assert account.seen == {"forfeit1": "no-analysis"}
    assert og.analysed == []
    assert allowance.analyses_left() == ogsync.DAILY_ANALYSES


def test_a_real_forfeit_has_no_play():
    forfeit = ('; [Site "OpenGammon"]\n\n5 point match\n\n Game 1\n gopreta: 0    hajjs: 0\n'
               "  1)                \n   Wins 5 points and the match\n")
    assert not ogsync.has_play(forfeit)
    assert ogsync.has_play(" Game 1\n  1) 52: 13/8 13/11              64: 24/14\n")


# --- analysing on this computer ---------------------------------------------------------


def test_local_analysis_never_touches_the_analysis_route(allowance):
    work = LocalWork()
    account = FakeAccount(analysis="local", floor=0, before=NOW)
    og = FakeOg([m("old", NOW - 400 * DAY, ply=0)], allowance)
    s = syncer(account, og, allowance, work=work)

    import threading

    def loop():
        while True:
            item = work.take()
            if item is not None:
                assert item["preset"] == "fast"
                work.finish(item, result=b"gvab:" + item["mat"].encode())
                return

    t = threading.Thread(target=loop)
    t.start()
    s.history_step()
    t.join(5)
    assert og.analysed == []
    assert account.inbox["old"]["gvab"].startswith(b"gvab:")
    assert account.inbox["old"]["analysis"] is None


def test_a_preset_the_engine_dropped_falls_back_to_its_default(allowance):
    account = FakeAccount(analysis="local")
    account.state["preset"] = "balanced"
    s = syncer(account, FakeOg([], allowance), allowance)
    assert s.preset() is None


def test_quitting_releases_a_sync_waiting_on_local_analysis():
    import threading

    work = LocalWork()
    out = {}

    def submit():
        try:
            work.submit("mat", None, "m1")
        except InterruptedError as e:
            out["e"] = e

    t = threading.Thread(target=submit)
    t.start()
    while not work.pending():
        pass
    work.close()
    t.join(5)
    assert "e" in out


# --- opengammon.com itself ---------------------------------------------------------


@respx.mock
def test_a_429_stops_every_request_until_it_has_passed(allowance):
    route = respx.get("https://opengammon.com/app/user/kit/matches/").mock(
        return_value=httpx.Response(429, json={"retry_after": 600}))
    og = OpenGammon(allowance, sleep=lambda s: None)
    with pytest.raises(ogsync.OgRateLimited):
        og.matches("kit", 0, NOW, 100)
    with pytest.raises(ogsync.OgRateLimited):
        og.matches("kit", 0, NOW, 100)
    assert route.call_count == 1
    assert allowance.blocked_for() == 600


@respx.mock
def test_the_list_is_asked_by_date_range_for_pvp_only(allowance):
    route = respx.get("https://opengammon.com/app/user/kit/matches/").mock(
        return_value=httpx.Response(200, json={"status": "success", "matches": [], "total": 0}))
    OpenGammon(allowance, sleep=lambda s: None).matches("kit", 5, 10, 100)
    params = route.calls.last.request.url.params
    assert params["date_range"] == "5,10"
    assert params["match_type"] == "pvp"


@respx.mock
def test_an_unfinished_analysis_reads_as_not_ready(allowance):
    respx.get("https://opengammon.com/app/match/q/analysis/").mock(
        return_value=httpx.Response(200, json={"analysis": {"status": "created"}}))
    assert OpenGammon(allowance, sleep=lambda s: None).analysis("q") is None
    assert allowance.analyses_left() == ogsync.DAILY_ANALYSES - 1


# --- the analysis window -----------------------------------------------------------


def _at(hour, minute=0):
    """An instant at this local time of day, whatever zone the tests run in."""
    return time.mktime((2026, 10, 4, hour, minute, 0, 0, 0, -1))


def test_the_window_is_read_on_this_computers_clock():
    assert window_wait(None, None, _at(12)) == 0
    assert window_wait(9 * 60, 17 * 60, _at(12)) == 0
    assert window_wait(9 * 60, 17 * 60, _at(8)) == 3600
    assert window_wait(9 * 60, 17 * 60, _at(17)) == 16 * 3600


def test_a_window_may_cross_midnight():
    night = (22 * 60, 7 * 60)
    assert window_wait(*night, _at(23)) == 0
    assert window_wait(*night, _at(3)) == 0
    assert window_wait(*night, _at(7)) == 15 * 3600
    assert window_wait(*night, _at(21, 30)) == 1800


def test_outside_the_window_local_sync_fetches_nothing(allowance):
    hour = time.localtime(NOW).tm_hour
    account = FakeAccount(analysis="local", floor=0, before=NOW)
    account.state.update(window_start=(hour + 2) % 24 * 60, window_end=(hour + 4) % 24 * 60)
    og = FakeOg([m("old", NOW - 400 * DAY), m("new", NOW - 3600)], allowance)
    s = syncer(account, og, allowance, work=LocalWork())
    assert s.window_wait() > 0
    assert s.history_step() is False
    s.check()
    assert og.lists == []
    assert account.taken() == {}


def test_a_window_never_holds_back_opengammons_analysis(allowance):
    hour = time.localtime(NOW).tm_hour
    account = FakeAccount(floor=0, before=NOW)
    account.state.update(window_start=(hour + 2) % 24 * 60, window_end=(hour + 4) % 24 * 60)
    s = syncer(account, FakeOg([m("new", NOW - 2 * DAY)], allowance), allowance)
    assert s.window_wait() == 0


# --- check now ------------------------------------------------------------------------


def test_a_press_cuts_the_wait_short(allowance):
    account = FakeAccount()
    presses = iter([False, True])
    account.og_wait = lambda wait: next(presses)
    s = syncer(account, FakeOg([], allowance), allowance)
    s._idle(3600)
    assert s.check_asked


def test_a_site_without_the_route_is_slept_on(allowance):
    account = FakeAccount()
    account.og_wait = lambda wait: None
    s = syncer(account, FakeOg([], allowance), allowance)
    slept = []
    s._sleep = slept.append
    s._idle(60)
    assert slept and not s.check_asked


def test_an_asked_check_runs_outside_the_window(allowance):
    hour = time.localtime(NOW).tm_hour
    account = FakeAccount(analysis="opengammon", before=None)
    account.state.update(analysis="local", window_start=(hour + 2) % 24 * 60,
                         window_end=(hour + 4) % 24 * 60)
    og = FakeOg([], allowance)
    s = syncer(account, og, allowance, work=LocalWork())
    s.check()
    assert og.lists == []
    s.check(asked=True)
    assert og.lists
