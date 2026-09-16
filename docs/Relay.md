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
`https://beta.gammonview.com/accounts/relay`. See `gvhelper/config.py`.

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
| GET | `/worker/next-job?wait=` | long poll, server caps `wait` at **30s** |
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
`live`, which is what the site's menu reads. A claim is first-wins under one
`UPDATE … WHERE status='queued'`, so two helpers on one account race safely.

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
