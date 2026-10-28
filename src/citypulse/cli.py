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
                proxy_headers=True, forwarded_allow_ips="*", log_level=os.environ.get("LOG_LEVEL", "info"))


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


if __name__ == "__main__":
    main()
