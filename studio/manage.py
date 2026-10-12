"""Command line tools for the site owner.

    python -m studio.manage make-admin EMAIL
    python -m studio.manage grant-credits EMAIL CENTS REASON

Uses the same settings as the website (STUDIO_DB_PATH and the other variables in studio/config.py).
"""
from __future__ import annotations

import argparse
import sys

from . import credits, db
from .config import load_settings


def _open_db() -> None:
    settings = load_settings()
    db.configure(settings.db_path)
    db.init([])  # makes sure the core tables exist; safe to run every time


def _find_user(email: str):
    with db.session() as conn:
        return conn.execute("SELECT id, email, display_name, is_admin FROM users WHERE email = ?",
                            (email.strip().lower(),)).fetchone()


def make_admin(email: str) -> int:
    user = _find_user(email)
    if user is None:
        print(f"No member has the email address {email}. They need to create an account first.", file=sys.stderr)
        return 1
    if user["is_admin"]:
        print(f"{user['email']} can already use the site owner tools.")
        return 0
    with db.session() as conn:
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["id"],))
    print(f"{user['email']} can now use the site owner tools at /account/admin.")
    return 0


def grant(email: str, cents_text: str, reason: str) -> int:
    try:
        cents = int(cents_text)
    except ValueError:
        print("CENTS must be a whole number of US cents, for example 500 for $5.00.", file=sys.stderr)
        return 2
    if cents <= 0:
        print("CENTS must be more than 0.", file=sys.stderr)
        return 2
    reason = reason.strip()
    if not reason:
        print("Give a reason. The member sees it in their credit history.", file=sys.stderr)
        return 2
    user = _find_user(email)
    if user is None:
        print(f"No member has the email address {email}.", file=sys.stderr)
        return 1
    credits.add(user["id"], cents, f"Gift from the site: {reason[:200]}", "manage")
    print(f"Gave {credits.format_cents(cents)} of credits to {user['email']}. "
          f"Their balance is now {credits.format_cents(credits.balance(user['id']))}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m studio.manage", description="Site owner tools.")
    commands = parser.add_subparsers(dest="command", required=True)
    p_admin = commands.add_parser("make-admin", help="Let a member use the site owner tools.")
    p_admin.add_argument("email")
    p_grant = commands.add_parser("grant-credits", help="Give credits to a member.")
    p_grant.add_argument("email")
    p_grant.add_argument("cents", help="Whole US cents, for example 500 for $5.00.")
    p_grant.add_argument("reason", nargs="+", help="Why. The member sees this.")
    args = parser.parse_args(argv)
    _open_db()
    if args.command == "make-admin":
        return make_admin(args.email)
    return grant(args.email, args.cents, " ".join(args.reason))


if __name__ == "__main__":
    sys.exit(main())
