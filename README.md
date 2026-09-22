# gammonview-helper

Analyze your backgammon matches on your own computer, and see the results on
[gammonview.com](https://gammonview.com).

GammonView analyses matches on a shared server. That server is shared, so the
deepest settings are not on its menu — a *World Class* run that takes a minute
on your laptop would take an hour of a queue everybody else is waiting in. The
helper moves that work to your machine: you keep using the website exactly as
before, and the analysis happens at home.

Nothing listens on a port. The helper dials out to gammonview.com, waits for
work **you** queued from your own browser, runs it, and sends the result back.
There is no inbound connection, no router setting, no firewall rule.

## Install

Needs [uv](https://docs.astral.sh/uv/) (or pipx, or a plain `pip install` into
a virtualenv).

```bash
uv tool install gammonview-helper
```

That pulls in the analysis engine and its neural networks — about 80 MB, and it
takes a minute or two the first time.

## Link this computer

```bash
gammonview-helper link
```

It prints a word and opens your browser. Sign in if you are asked, and pick the
word it printed out of the three you are shown. You will get an email saying
the computer was linked.

If the three words do not include the one on your screen, **something is
wrong** — close the page and start again. That is the check working.

## Run it

```bash
gammonview-helper run
```

Leave it running. Analyses you start on gammonview.com will now go to this
computer, and the deeper presets appear in the menu. If the helper is not
running, the website falls back to the shared server on its own — you never get
an error for having closed it.

You can start it before linking. An unlinked helper waits for a credential
rather than giving up, so the usual order — install, let it start, then link in
the browser — needs no third step telling it to look again; it picks the link
up within a few seconds. The same is true if you unlink and re-link later.

### Keeping it running (macOS)

`run` stops when you close the terminal. To have it start at login and stay up:

```bash
deploy/install-login-item.sh
```

That registers a launchd agent pointing at the installed `gammonview-helper`,
logging to `~/Library/Logs/GammonView/helper.log`. Re-run it after an upgrade —
it boots the old job out first, so you never end up with two pollers sharing one
worker id, and it keeps whatever site the installed agent already pointed at.
To move a machine between sites, say so explicitly:

```bash
GAMMONVIEW_SITE=https://gammonview.com deploy/install-login-item.sh
```

It says when it moves one, because a credential is per site: the machine has to
be linked to the new one separately.

```bash
deploy/install-login-item.sh stop     # stop it, and leave it stopped
tail -f ~/Library/Logs/GammonView/helper.log
```

Installing always leaves the helper running, so a `stop` does not survive the
next install. After stopping, the website keeps saying **Connected** for up to
two minutes — a helper stops by going quiet, and the relay waits that long
before believing a silence.

The eventual installer does this for you; the script exists because the machine
this is developed on needed it first, and `deploy/com.gammonview.helper.plist`
records the four choices in it that are not boilerplate.

## The other two commands

```bash
gammonview-helper status     # what is configured, and is the link still alive
gammonview-helper unlink     # forget this computer's credentials
```

`unlink` is local. To remove the computer from your account, use **Unlink** in
your account settings on the website — the helper's credentials deliberately
cannot reach your account, so they cannot revoke themselves.

## Settings

Everything lives in one directory, which is the whole of the uninstall:

| | |
|---|---|
| macOS | `~/Library/Application Support/GammonView/` |
| Windows | `%LOCALAPPDATA%\GammonView\` |
| Linux | `~/.config/gammonview/` |

The credential is kept in your platform's credential store (Keychain, Credential
Manager, Secret Service), not in that directory. `status` says which store it
actually got — on a machine with no credential store it falls back to a
`0600` file, and tells you so.

`config.json` takes a few optional keys:

| key | default | what it does |
|---|---|---|
| `jobs` | `0` — the engine decides | how many of a match's decisions run in parallel |
| `threads` | `0` — the engine decides | engine threads inside each of those |
| `nice` | 10 | how hard the helper tries to stay out of your way |

The parallelism is sized by the engine, from measurements it keeps for the
purpose; set either to a number if you want a hard cap, and it will be kept.
`nice` is what makes the helper something you forget is running — it yields to
whatever you are actually doing, and every engine worker inherits it.

## Licence

MIT. The engine ([bgsage](https://pypi.org/project/bgsage/)) is MPL-2.0 and is
installed as an ordinary dependency.

## Development

```bash
uv run pytest -q -m "not slow and not contract"
```

Two markers are deselected there. `slow` runs a real analysis and needs a
minute of CPU. `contract` talks to a **live** relay and is opt-in:

```bash
GAMMONVIEW_CONTRACT_BASE=https://beta.gammonview.com uv run pytest -m contract
```

The other end of that protocol — the relay itself — is part of the
gammonview.com service and is not open source. `docs/Relay.md` documents the
seam from this side, says which revision this client implements, and explains
why the contract carries a number of its own.
