import argparse
import os
import sys

from . import auth, migrate
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
    elif args.cmd == "reset-admin":
        with connect() as conn:
            auth.reset_admin(conn)
        print("Admin password cleared. Restart the server; it will print a new setup code.")
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
