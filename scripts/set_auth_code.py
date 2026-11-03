"""One-time setup: set the 8-digit authentication code.

Reads the code from terminal input exactly once (hidden, never
echoed), derives a PBKDF2-HMAC-SHA256 hash with a fresh random
salt, and stores ONLY the salt + hash in the auth_codes table.
The plaintext code is never printed, never logged, and never
written to any file. Re-running this script rotates the code: the
new row becomes the current one and old rows stop working.

Run once on the server machine:
    PYTHONPATH=. python scripts/set_auth_code.py

The script verifies by name/length/masked form only: on success it
prints that a code hash was stored (and when), nothing else.
Requires schema_security.sql applied and DATABASE_URL set.
Exit code 0 iff a hash was stored.
"""

import getpass
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn
from app.security import hash_code


def main():
    code = getpass.getpass("Enter the 8-digit authentication code: ")
    if not re.fullmatch(r"\d{8}", code or ""):
        print("ERROR: code must be exactly 8 digits.")
        return 1
    confirm = getpass.getpass("Enter it again to confirm: ")
    if confirm != code:
        print("ERROR: codes do not match. Nothing was stored.")
        return 1
    salt_hex = secrets.token_hex(16)
    digest = hash_code(code, salt_hex)
    # Drop the plaintexts from memory as soon as the hash exists.
    code = None
    confirm = None
    with get_conn() as conn:
        conn.execute("DELETE FROM auth_codes")
        row = conn.execute(
            "INSERT INTO auth_codes (salt, code_hash) VALUES (%s, %s) "
            "RETURNING created_at",
            (salt_hex, digest),
        ).fetchone()
        conn.commit()
    print(
        "Stored: 8-digit code hash saved "
        f"(salt {len(salt_hex)} hex chars, hash {len(digest)} hex chars, "
        f"stored at {row[0] if row else 'unknown'})."
    )
    print("The code itself was not printed or saved anywhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
