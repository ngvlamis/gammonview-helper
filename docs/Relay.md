# The relay protocol, revision 1

What this package talks to, and the one document that has to stay true when the
two halves of it are maintained apart.

The server is `accounts/gvaccounts/relay.py` in the GammonView repository,
which is **private**. This client is public. That asymmetry is intentional — a
program that runs on other people's computers should be readable by them, and a
service that holds other people's accounts should not be — but it has a cost:
neither side can be edited with the other on screen, and a mismatch does not
announce itself. A helper against a changed relay pairs successfully, polls
happily, and never claims a job.

So: a contract revision, `RELAY_CONTRACT` in `gvhelper/client.py`, sent in the
`User-Agent` on every request as `gammonview-helper/<version> (relay/<n>)`.
Bump it when a route, a field, or a status code changes meaning. Do not bump it
for a release — the package version is on its own line for exactly that reason.

## Base

Every path below is relative to `{site}{api_path}/relay`, which by default is
`https://gammonview.com/accounts/relay`. See `gvhelper/config.py`.

## Pairing — no credential, and two secrets that must not be confused

| Method | Path | Who calls it |
| --- | --- | --- |
| POST | `/pair/start` | helper |
| GET | `/pair/{code}` | browser |
| POST | `/pair/{code}/confirm` | browser |
| POST | `/pair/poll` | helper |

`start` returns **two** distinct strings. `code` goes in the URL the helper
opens in a browser; `secret` never leaves the helper and is what `poll`
presents. Handing one value to both jobs would mean anyone who saw the link
could collect the token it was about to mint. `PairingClient` keeps them apart
by construction and `tests/test_client.py` asserts the secret is never put in a
URL.

A pairing lives **60 seconds** (`PAIR_TTL_SECONDS`). The browser is shown three
words and must pick the one the helper printed; a wrong pick fails the pairing
rather than offering another go.

## Work — a worker session, and nothing else

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/worker/hello` | name, version, engine, presets |
| GET | `/worker/next-job?wait=` | long poll; server clamps `wait` to **25s** |
| POST | `/worker/jobs/{id}/progress` | also the lease renewal |
| POST | `/worker/jobs/{id}/result` | raw `application/octet-stream` body |
| POST | `/worker/jobs/{id}/error` | message truncated to 500 chars |

These carry `Authorization: Bearer <worker token>`, a session whose `scope` is
`worker`. The account routes reject it — the partition between `current_user`
and `current_worker` is the security boundary the whole design rests on, which
is why `unlink` here is local-only: this token cannot delete its own row.

Three server timings this client is built around. A job's lease is **300s**
(`LEASE_SECONDS`) and a progress report renews it, so progress *is* the
heartbeat and `daemon.py` posts one every 5 seconds whether or not the number
moved. A worker unheard from for **120s** (`WORKER_FRESH_SECONDS`) stops being
`live`, which is what the site's menu reads — and **an empty poll counts as
being alive**, which it did not until revision 2; a helper waiting quietly for
work is the normal state of a working helper. A claim is first-wins under one
`UPDATE … WHERE status='queued'`, so two helpers on one account race safely.

**Long polls live under other people's proxies.** The server holds `next-job`
for at most 25 seconds and this client asks for 20, both of them under the 30
that read timeouts commonly default to. A poll held for exactly as long as the
proxy will wait is a coin flip between an answer and an HTML 504 from
something the helper has never heard of. So `next_job` also treats 502, 503,
504, 408 and 524 as *no work* rather than as errors — nothing was claimed, so
nothing is lost by asking again — with a five-second floor, because a proxy
whose upstream is down answers instantly and the alternative is a busy loop.

The match arrives base64-encoded inside JSON; the result goes back as a raw
gzipped `.gvab` body. Not symmetric, and deliberately: the job envelope has
fields beside the bytes, the result is only bytes.

## What this client does not use

`/workers`, `/workers/{id}`, `/analyze`, `/analyze/{id}`, and
`/analyze/{id}/result` are the browser's half of the relay, taking
`current_user`. They are listed here so that "the helper does not call this"
is written down rather than inferred.

## Revisions

**1** — the protocol as first deployed to beta, 2026-09-16. Pairing with
word-confirmation, the five worker routes above, `scope='worker'` sessions.

**2** — 2026-09-16, the same day, after the first live pairing found two things
no local test could. `next-job` now refreshes `last_seen_at`, so an idle helper
stays `live` instead of vanishing from the site after two minutes while its own
`status` command still reported a working link. And `wait` is **clamped**
rather than validated: it used to be `le=MAX_WAIT_SECONDS`, so lowering that
cap — which revision 2 does, 30 to 25 — would have answered every helper still
asking for 30 with a 422. A server upgrade must not break clients on machines
nobody here can update.

A revision-1 client works unchanged against a revision-2 relay. The number is
for reading logs and for knowing what to check, not a handshake.
