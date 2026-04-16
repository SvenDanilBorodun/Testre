"""Bootstrap the first admin account.

Creates a new Supabase Auth user with a synthetic email, then promotes the
public.users row to role='admin'. Run once after the 002_accounts.sql
migration is applied.

Usage (from robotis_ai_setup/):
    cd cloud_training_api && cp .env.example .env  # fill in real values
    python ../scripts/bootstrap_admin.py --username admin --full-name "Sven"
    # You'll be prompted for a password.

The script uses the SERVICE_ROLE key to create the auth user. Keep that key
private.
"""

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client


SYNTHETIC_EMAIL_DOMAIN = "edubotics.local"
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


def main():
    parser = argparse.ArgumentParser(description="Create the first admin account.")
    parser.add_argument("--username", required=True, help="Login username (lowercase, 3-32 chars)")
    parser.add_argument("--full-name", required=True, help="Display name")
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parent.parent / "cloud_training_api" / ".env"),
        help="Path to .env (defaults to cloud_training_api/.env)",
    )
    args = parser.parse_args()

    username = args.username.strip().lower()
    if not USERNAME_RE.match(username):
        sys.exit("Username must be 3-32 chars, lowercase letters/digits/._-")

    env_path = Path(args.env_file)
    if not env_path.exists():
        sys.exit(f".env not found at {env_path}")
    load_dotenv(env_path)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be in the .env")

    password = getpass.getpass("New admin password (min 6 chars): ")
    password_confirm = getpass.getpass("Confirm password: ")
    if password != password_confirm:
        sys.exit("Passwords do not match")
    if len(password) < 6:
        sys.exit("Password must be at least 6 characters")

    client = create_client(url, key)
    email = f"{username}@{SYNTHETIC_EMAIL_DOMAIN}"

    print(f"\nCreating auth user {email}...")
    try:
        created = client.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        )
    except Exception as e:
        sys.exit(f"auth.admin.create_user failed: {e}")

    user = getattr(created, "user", None)
    if user is None:
        sys.exit("Did not get a user back from Supabase.")
    user_id = user.id
    print(f"  auth user created: {user_id}")

    print("Promoting to admin role...")
    try:
        client.table("users").update(
            {
                "role": "admin",
                "username": username,
                "full_name": args.full_name.strip(),
            }
        ).eq("id", user_id).execute()
    except Exception as e:
        print(f"WARN: profile update failed, rolling back auth user: {e}")
        try:
            client.auth.admin.delete_user(user_id)
        except Exception as del_err:
            print(f"  rollback failed: {del_err}")
        sys.exit(1)

    print("\nDone! You can now log in to the web dashboard with:")
    print(f"  Username: {username}")
    print(f"  Password: (what you entered)")


if __name__ == "__main__":
    main()
