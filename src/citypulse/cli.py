import argparse
import os
import sys

from . import auth, history, migrate
from .config import get_settings, require_sim_city
from .db import connect


def _check_sim() -> None:
    cfg = get_settings()
    if cfg.role in ("sim", "all"):
        require_sim_city(cfg)         # fail before anything starts, with the fix in the message


def _serve() -> None:
    import uvicorn

    _check_sim()

    uvicorn.run("citypulse.api:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("CITYPULSE_FORWARDED_ALLOW_IPS", "127.0.0.1"),
                log_level=os.environ.get("LOG_LEVEL", "info"))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="citypulse", description="Smart-city sensor platform: gateway, simulator, status engine.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("serve", help="run the server (CITYPULSE_ROLE=api|sim|all)")
    sub.add_parser("start", help="migrate, then serve (the container default)")
    sh = sub.add_parser("seed-history", help="write deterministic demo history for the pattern memory (optional; GUIDE 7.14)")
    sh.add_argument("--cities", default=",".join(history.DEFAULT_CITIES), help="comma-separated built-in city ids (default: %(default)s)")
    sh.add_argument("--days", type=int, default=history.DEFAULT_DAYS, help="weeks of history to write, in days (default: %(default)s)")
    sh.add_argument("--reset", action="store_true", help="first delete the episodes this command wrote earlier (trigger 'seed'); live ones stay")
    sa = sub.add_parser("aidb-setup", help="operator step for AIDB mode: models, incident pipeline, semantic knowledge base, purpose, agent, governance view, "
                                           "grants, seals (needs an admin connection string; prompted, or CITYPULSE_AIDB_ADMIN_DSN)")
    sa.add_argument("--model", default="", help="use this already registered AIDB model for the agent instead of registering the llm-gateway one")
    sa.add_argument("--no-validate", action="store_true", help="do not let AIDB call the gateway while registering the model")
    sa.add_argument("--reseal", action="store_true", help="accept the current state of definitions whose seal no longer matches (inspect them first)")
    st = sub.add_parser("aidb-status", help="AIDB mode: what is requested, what is set up, what fails its seal (exit 1 when something is wrong)")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("reset-admin", help="forget the admin password so setup can run again")
    args = p.parse_args(argv)
    if args.cmd == "migrate":
        done = migrate.run()
        print("applied:", ", ".join(done) if done else "nothing to do")
    elif args.cmd == "serve":
        _serve()
    elif args.cmd == "start":
        # The simulator-only role has no schema of its own to create, but migrating is idempotent and safe.
        _check_sim()
        from apgkit.sealed import Sealed

        if not Sealed.from_env().enabled:      # sealed boot: no database URL until the operator unseals; the unseal callback migrates
            migrate.run()
        _serve()
    elif args.cmd == "seed-history":
        migrate.run()
        try:
            with connect() as conn:
                out = history.seed(conn, [c.strip() for c in args.cities.split(",") if c.strip()], args.days, reset=args.reset)
        except ValueError as exc:
            sys.exit(f"seed-history: {exc}")
        print(f"seeded {out['inserted']} episodes ({out['windows']} windows x {out['districts']} districts x {len(out['cities'])} cities: "
              f"{', '.join(out['cities'])}), labelled {out['labelled']} outcomes"
              + ("" if out["similarity_search"] else f"\nnote: similarity search is off ({out['note']}); the fingerprints are stored and will be searchable once pgvector is installed"))
    elif args.cmd == "aidb-setup":
        sys.exit(_aidb_setup(args))
    elif args.cmd == "aidb-status":
        sys.exit(_aidb_status(args))
    elif args.cmd == "reset-admin":
        with connect() as conn:
            auth.reset_admin(conn)
        if os.environ.get("CITYPULSE_OIDC_ISSUER", "").strip():
            print("Admin password cleared. Single sign-on is on, so no setup code is printed: sign in with SSO as an admin and set a new "
                  "password under Settings, or sign in with CITYPULSE_API_KEY if one is configured.")
        else:
            print("Admin password cleared. Restart the server; it will print a new setup code.")
    else:
        sys.exit(2)


def _unlock_vault(conn, *, create: bool, prompt: bool = True) -> bool:
    """Open the seal key for this command: the platform secret (CITYPULSE_ENCRYPTION_KEY), else the passphrase. `create` makes the vault when none
    exists yet (aidb-setup only). Under sealed boot the passphrase is the unseal passphrase, the one that opens the vault at start-up. False says
    why on stderr."""
    import getpass

    from .core import vault

    if vault.is_unlocked():
        return True
    if vault.is_initialized(conn):
        if vault.try_unlock_with_platform_secret(conn):
            return True
        if not prompt or not sys.stdin.isatty():
            return False
        if vault.unlock_with_password(conn, getpass.getpass("Seal key passphrase (the sealed-boot passphrase, not shown): ") or ""):
            return True
        print("that passphrase does not open the seal key", file=sys.stderr)
        return False
    if not create:
        return False
    if vault.initialize_with_platform_secret(conn):
        conn.commit()
        return True
    if not sys.stdin.isatty():
        print("no seal key exists and there is no CITYPULSE_ENCRYPTION_KEY to create one under; run aidb-setup in a terminal", file=sys.stderr)
        return False
    pw = getpass.getpass("Seal key passphrase to create (use the sealed-boot passphrase if you use sealed boot; not shown, not stored): ")
    if len(pw) < 12:
        print("use at least 12 characters", file=sys.stderr)
        return False
    vault.initialize(conn, pw)
    conn.commit()
    return True


def _aidb_setup(args) -> int:
    """Everything AIDB mode needs that the app's own role must not be able to do. The admin connection string is read from the prompt (or from
    CITYPULSE_AIDB_ADMIN_DSN for this one command), used for this command only and never printed, stored or logged."""
    import getpass

    import psycopg
    from psycopg.rows import dict_row

    from . import aidb_mode
    from .core import vault as core_vault

    dsn = os.environ.get(aidb_mode.ADMIN_DSN_VAR) or getpass.getpass("AIDB admin connection string (not shown, not stored): ")
    if not dsn.strip():
        print("no admin connection string given", file=sys.stderr)
        return 2
    with connect() as conn:
        if not _unlock_vault(conn, create=True):
            print("the seal key could not be opened or created", file=sys.stderr)
            return 1
        role = conn.execute("SELECT current_user AS u").fetchone()["u"]
        try:
            admin = psycopg.connect(dsn.strip(), row_factory=dict_row)
        except psycopg.Error as exc:
            print(f"could not connect with the admin connection string: {type(exc).__name__}", file=sys.stderr)
            return 1
        try:
            steps = aidb_mode.setup(admin, conn, role, vault=core_vault, model=args.model, validate=False if args.no_validate else None,
                                    reseal=args.reseal)
        except (aidb_mode.SetupError, aidb_mode.aidb.AidbError) as exc:
            admin.rollback()
            print(f"aidb-setup stopped: {str(exc).splitlines()[0][:300]}", file=sys.stderr)
            if isinstance(exc, aidb_mode.aidb.AidbTampered):
                print("a sealed definition was changed outside this app; inspect it, then re-run with --reseal to accept the current state", file=sys.stderr)
            return 1
        except psycopg.Error as exc:
            admin.rollback()
            print(f"aidb-setup stopped: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}", file=sys.stderr)
            return 1
        finally:
            admin.close()
        conn.commit()
    for s in steps:
        print(s)
    print(f"done: AIDB mode is set up and sealed. Switch it on with {aidb_mode.BACKEND_VAR}=aidb, then restart.")
    return 0


def _aidb_status(args) -> int:
    import json

    from . import aidb_mode

    with connect() as conn:
        _unlock_vault(conn, create=False)           # without it the seals cannot be checked: the report then says so
        s = aidb_mode.inspect(conn)
        s["governance"] = aidb_mode.recent_decisions(conn) if s.get("aidb", {}).get("available") else {"available": False, "reason": "AIDB is not available"}
        conn.rollback()
    problems = s["state"] != "ready" or any(not f["ready"] for f in s["features"].values())
    if args.json:
        print(json.dumps(s, indent=2, default=str))
        return 1 if problems else 0
    print("requested:", ", ".join(f"{k}={v}" for k, v in s["requested"].items()))
    print(f"AIDB: {'available, version ' + str(s['aidb']['version']) if s.get('aidb', {}).get('available') else 'not available'}; state: {s['state']}"
          + (f" ({s['reason']})" if s["reason"] else ""))
    for t in s["tampered"]:
        print(f"  TAMPERED {t['kind']} {t['name']}: {t['problem']}")
    for name, f in s["features"].items():
        print(f"  {name:<10} {'ready' if f['ready'] else 'not ready: ' + f['reason']}")
    g = s["governance"]
    print("governance: " + (f"{g['denied']} denied decision(s) in the last {g['window']}" if g.get("available") else f"not available ({g.get('reason', '')})"))
    return 1 if problems else 0


if __name__ == "__main__":
    main()
