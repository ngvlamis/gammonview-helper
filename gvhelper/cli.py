# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""`gammonview-helper` -- link this computer, then analyse on it.

Four commands, and the first one is the only one most people will ever type:

    gammonview-helper link       # pair this machine with a GammonView account
    gammonview-helper run        # poll for work (what the login item runs)
    gammonview-helper status     # what is configured, and is the link alive
    gammonview-helper unlink     # forget this machine's credentials

Written for a terminal a non-technical person has been talked into opening. The
output is sentences rather than log lines, the word to confirm is unmissable,
and every failure says what to do next instead of only what went wrong -- the
audience for this app skews older and non-technical, and a stack trace is where
they stop.
"""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser

from . import __version__
from .client import PairingClient, RelayError, Unauthorized, WorkerClient
from .config import AUTO, Config, load, machine_name, platform_name, save
from .runner import available_presets, engine_version
from .store import backend_name, clear_token, load_token, save_token


def _say(message: str = "") -> None:
    print(message, flush=True)


def _banner(word: str) -> None:
    """The word, drawn so that it cannot be mistaken for anything else.

    This is the security-critical line of the whole install: the user is about
    to match it against three words in a browser, and a word they skim past is
    a word they will guess at. So it gets a box, blank lines, and nothing near
    it competing for attention.
    """
    line = "─" * (len(word) + 8)
    _say()
    _say(f"    ┌{line}┐")
    _say(f"    │    {word}    │")
    _say(f"    └{line}┘")
    _say()


def cmd_link(cfg: Config, args: argparse.Namespace) -> int:
    """The device flow, from this side.

    Note what this command never learns: which account it is being linked to.
    The helper says what machine it is and shows a word; a signed-in person
    elsewhere decides whether that machine may analyse for them. That is what
    lets one unmodified installer work for anybody.
    """
    pairing = PairingClient(cfg)
    machine = args.name or machine_name()
    platform = platform_name()

    try:
        offer = pairing.start(machine, platform)
    except RelayError as e:
        _say(f"Could not start linking: {e}")
        return 1

    url = cfg.link_url(offer["code"])
    expires_in = int(offer.get("expires_in") or 60)

    _say(f"Linking this computer ({machine}, {platform}) to GammonView.")
    _say("Your browser should open. Sign in if you are asked, then choose this word:")
    _banner(offer["word"])
    _say(f"You have about {expires_in} seconds. The page is at:")
    _say(f"  {url}")
    _say()

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - a machine with no browser
            pass

    # Poll until it is confirmed or the offer expires. The relay holds each poll
    # open for up to 30s, so a 60s window is two round trips and there is no
    # busy-waiting to throttle. A deadline of our own rather than trusting the
    # server's 404: the message a user needs when nothing happened is "it timed
    # out, run it again", which the 404 alone does not distinguish from "that
    # code was wrong".
    deadline = time.monotonic() + expires_in + 5
    while time.monotonic() < deadline:
        try:
            answer = pairing.poll(offer["code"], offer["secret"])
        except RelayError as e:
            # The relay deletes the row on a wrong word, so this is the honest
            # end of the unhappy path as well as the expiry. Both mean: start
            # again, and it is one command.
            _say(f"Linking did not complete: {e}")
            _say("Run `gammonview-helper link` to try again.")
            return 1
        if answer.get("status") == "linked":
            save_token(cfg, answer["token"])
            cfg.worker_id = answer.get("worker_id")
            save(cfg)
            _say("This computer is linked.")
            _say(f"Credentials stored in: {backend_name(cfg)}")
            _say()
            _say("Start analysing with:")
            _say("  gammonview-helper run")
            return 0

    _say("Nobody confirmed in time.")
    _say("Run `gammonview-helper link` to try again.")
    return 1


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    from .daemon import serve

    token = load_token(cfg)
    if not token:
        _say("This computer is not linked yet.")
        _say("Run `gammonview-helper link` first.")
        return 2
    return serve(cfg, token, once=args.once)


def _parallelism(cfg: Config) -> str:
    """The `jobs x threads` line, with `0` spelt out.

    Printed for somebody diagnosing "why is this slow", so a bare `0 jobs x 0
    threads` is the one thing it must not say -- zero reads as *none* and the
    setting means the opposite.
    """
    jobs = "auto" if cfg.jobs == AUTO else str(cfg.jobs)
    threads = "auto" if cfg.threads == AUTO else str(cfg.threads)
    sized = " (sized by the engine)" if AUTO in (cfg.jobs, cfg.threads) else ""
    return f"{jobs} jobs x {threads} threads{sized}"


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    """What is configured, and -- if asked -- whether the link still works.

    The check is a real request, because every interesting way this breaks is
    invisible from here: a token revoked in account settings, a site that moved,
    an account deleted. Local state will look perfect in all three.
    """
    token = load_token(cfg)
    _say(f"gammonview-helper {__version__}")
    _say(f"  computer   : {machine_name()} ({platform_name()})")
    _say(f"  site       : {cfg.api_base}")
    _say(f"  linked     : {'yes' if token else 'no'}")
    if cfg.worker_id:
        _say(f"  worker id  : {cfg.worker_id}")
    _say(f"  credentials: {backend_name(cfg)}")
    _say(f"  engine     : {engine_version()}")
    _say(f"  presets    : {', '.join(available_presets()) or '(engine not installed)'}")
    _say(f"  parallelism: {_parallelism(cfg)}, nice {cfg.nice}")

    if not token:
        return 1
    client = WorkerClient(cfg, token)
    try:
        # `hello` rather than a dedicated ping: it is idempotent, it is the call
        # the daemon makes at startup anyway, and making it here means `status`
        # also refreshes what the site knows this machine can do.
        client.hello(machine_name(), __version__, engine_version(), available_presets())
        _say("  link       : working")
        return 0
    except Unauthorized:
        _say("  link       : this computer is no longer linked (unlink it here and link again)")
        return 1
    except RelayError as e:
        _say(f"  link       : could not reach the relay ({e})")
        return 1
    finally:
        client.close()


def cmd_unlink(cfg: Config, args: argparse.Namespace) -> int:
    """Forget the credentials on this machine.

    Local only, and the message is careful to say so. The helper holds a worker
    token, which by design cannot reach the account routes -- so it cannot
    remove its own row from account settings, and claiming otherwise would leave
    someone believing they had revoked something they had not. Revocation is the
    Unlink button on the website; this is the half that can be done from here.
    """
    clear_token(cfg)
    cfg.worker_id = None
    save(cfg)
    _say("This computer has forgotten its GammonView credentials.")
    _say("It will not receive any more matches.")
    _say()
    _say("The machine may still be listed in your account settings on the website.")
    _say("Use Unlink there to remove it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gammonview-helper",
        description="Analyze GammonView matches on your own computer.",
    )
    parser.add_argument("--version", action="version", version=f"gammonview-helper {__version__}")
    parser.add_argument(
        "--site",
        help="GammonView site to talk to (default: %s)" % Config().site,
    )
    sub = parser.add_subparsers(dest="command")

    link = sub.add_parser("link", help="link this computer to a GammonView account")
    link.add_argument("--name", help="what to call this computer on the confirmation screen")
    link.add_argument(
        "--no-browser",
        action="store_true",
        help="print the link instead of opening it (for a machine with no browser)",
    )
    link.set_defaults(func=cmd_link)

    run = sub.add_parser("run", help="wait for matches to analyze")
    run.add_argument(
        "--once",
        action="store_true",
        help="do one poll and exit, instead of running until stopped",
    )
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show configuration and check the link")
    status.set_defaults(func=cmd_status)

    unlink = sub.add_parser("unlink", help="forget this computer's credentials")
    unlink.set_defaults(func=cmd_unlink)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    cfg = load()
    if args.site:
        cfg.site = args.site
    return args.func(cfg, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
