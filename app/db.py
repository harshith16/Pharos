"""Database pool. Connection details come only from config (env names).

No secrets are printed or stored here.

Row Level Security: schema_security.sql gates every SELECT on the
fact tables behind two per-request session variables. scoped_conn()
is the only way engine queries should open a transaction: it sets
app.current_role and app.sensitive_unlocked with SET LOCAL (so the
variables die with the transaction) before yielding. Role and unlock
values are validated against fixed allow-lists and passed as bound
parameters, never interpolated.

Two scopes exist, and they must not be mixed up:
- sender scope: scoped_conn(role, unlocked=is_unlocked) for every
  WhatsApp answer transaction — RLS hides sensitive rows unless
  the code gate opened them, and the role filter still applies.
- operator scope: scoped_conn("ceo", unlocked=True) for intake
  writes (WhatsApp media, /upload) and restricted-row seeds —
  Postgres checks INSERT ... RETURNING against the SELECT policy,
  so a finance/technical row cannot be written on an unscoped
  connection. General rows write anywhere. Operator scope is for
  server-side writes only, never for sender reads.
"""

import threading
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from app import config

_pool = None
_pool_conninfo = None
_lock = threading.Lock()


def get_pool() -> ConnectionPool:
    """Return the shared pool, creating it lazily in a thread-safe way."""
    global _pool, _pool_conninfo
    current_url = config.DATABASE_URL
    if _pool is None or _pool_conninfo != current_url:
        with _lock:
            if _pool is None or _pool_conninfo != current_url:
                if _pool is not None:
                    try:
                        _pool.close()
                    except Exception:
                        pass
                _pool = ConnectionPool(conninfo=current_url)
                _pool_conninfo = current_url
    return _pool


@contextmanager
def get_conn():
    """Yield one connection from the pool.

    Commits on clean exit, rolls back on exception, and always returns
    the connection to the pool. Callers in app.store commit their own
    work, so this commit is normally a no-op.
    """
    pool = get_pool()
    with pool.connection() as conn:
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


_KNOWN_ROLES = ("ceo", "cfo", "cto", "employee", "manager")


def _normalize_role(role) -> str:
    """Whitelisted role literal; anything unknown becomes employee
    (least privilege), mirroring the app role predicates."""
    if isinstance(role, str) and role.strip().lower() in _KNOWN_ROLES:
        return role.strip().lower()
    return "employee"


@contextmanager
def scoped_conn(role, unlocked: bool = False):
    """Yield a connection with the RLS session variables set.

    SET LOCAL binds the variables to the current transaction only:
    the pool returns the connection clean. `unlocked` must be True
    only after the sender passed the 8-digit code gate (see
    app.security); it never comes from the question text.
    """
    pool = get_pool()
    with pool.connection() as conn:
        try:
            conn.execute(
                "SELECT set_config('app.current_role', %s, true)",
                (_normalize_role(role),),
            )
            conn.execute(
                "SELECT set_config('app.sensitive_unlocked', %s, true)",
                ("on" if unlocked else "off",),
            )
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
