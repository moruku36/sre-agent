"""Local synthetic delivery of the v1 handoff produced by sre_mvp."""

import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import sys

import sre_mvp


LEDGER_SCHEMA = """CREATE TABLE handoff_deliveries (
    delivery_id TEXT NOT NULL PRIMARY KEY,
    handoff_json TEXT NOT NULL,
    acknowledged INTEGER NOT NULL CHECK (acknowledged IN (0, 1))
)"""


def _sha256_hex(text):
    return sha256(text.encode("utf-8")).hexdigest()


def _identity(path):
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_dev, status.st_ino


def _sqlite_files(path):
    paths = {path}
    paths.update(Path(f"{path}{suffix}").resolve() for suffix in ("-journal", "-wal", "-shm"))
    identities = {_identity(name) for name in paths}
    identities.discard(None)
    return paths, identities


def _separate_paths(db_path, ledger_path, policy_path=None):
    db, ledger = Path(db_path).resolve(), Path(ledger_path).resolve()
    db_paths, db_files = _sqlite_files(db)
    ledger_paths, ledger_files = _sqlite_files(ledger)
    if db_paths & ledger_paths or db_files & ledger_files:
        raise ValueError("signal DB and ledger must be separate files, including SQLite sidecars")

    if policy_path is not None:
        # Even a read-only SQLite connection can touch sidecars.
        policy = Path(policy_path).resolve()
        if policy in (db_paths | ledger_paths) or _identity(policy) in (db_files | ledger_files):
            raise ValueError("cost policy must be separate from both databases and their SQLite sidecars")

    return db, ledger


def _connect(ledger, mode):
    return sqlite3.connect(f"{ledger.as_uri()}?mode={mode}", uri=True)


def _check_schema(conn, *, initialize=False):
    # SQLite's implicit PK index has no SQL. Reject any other user objects.
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
    ).fetchall()
    if not objects:
        if initialize:
            conn.execute(LEDGER_SCHEMA)
        return initialize
    if objects != [("table", "handoff_deliveries", LEDGER_SCHEMA)]:
        raise ValueError("not a synthetic handoff ledger")
    return True


def _validate_stored_row(row):
    delivery_id, handoff_json, acknowledged = row
    if not isinstance(handoff_json, str) or _sha256_hex(handoff_json) != delivery_id:
        raise ValueError("stored handoff does not match its delivery ID")
    if acknowledged not in (0, 1):
        raise ValueError("invalid stored acknowledgment")
    return {
        "delivery_id": delivery_id,
        "handoff_json": handoff_json,
        "acknowledged": bool(acknowledged),
    }


def _delivery_row(conn, delivery_id):
    return conn.execute(
        "SELECT delivery_id, handoff_json, acknowledged "
        "FROM handoff_deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()


def _offer(db, ledger, policy_path, as_of):
    policy = sre_mvp.load_cost_policy(policy_path)
    snapshot = sre_mvp.synthetic_handoff(db, policy, as_of)
    handoff_json = sre_mvp._encode_cost_json(snapshot)
    delivery_id = _sha256_hex(handoff_json)

    with closing(_connect(ledger, "rwc")) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _check_schema(conn, initialize=True)
        row = _delivery_row(conn, delivery_id)
        if row is None:
            row = (delivery_id, handoff_json, 0)
            conn.execute(
                "INSERT INTO handoff_deliveries (delivery_id, handoff_json, acknowledged) "
                "VALUES (?, ?, ?)",
                row,
            )
        delivery = _validate_stored_row(row)
        if delivery["handoff_json"] != handoff_json:
            raise ValueError("delivery ID already refers to a different handoff")
    return delivery


def _pull(ledger):
    if _identity(ledger) is None:
        return None
    with closing(_connect(ledger, "ro")) as conn, conn:
        conn.execute("BEGIN")
        if not _check_schema(conn):
            return None
        row = conn.execute(
            "SELECT delivery_id, handoff_json, acknowledged FROM handoff_deliveries "
            "WHERE acknowledged = 0 ORDER BY rowid LIMIT 1"
        ).fetchone()
        return _validate_stored_row(row) if row is not None else None


def _ack(ledger, delivery_id):
    if re.fullmatch(r"[0-9a-f]{64}", delivery_id) is None:
        raise ValueError("delivery ID must be 64 lower-case hex characters")
    if _identity(ledger) is None:
        raise ValueError("unknown delivery ID")

    with closing(_connect(ledger, "rw")) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _delivery_row(conn, delivery_id) if _check_schema(conn) else None
        if row is None:
            raise ValueError("unknown delivery ID")
        delivery = _validate_stored_row(row)
        if not delivery["acknowledged"]:
            conn.execute(
                "UPDATE handoff_deliveries SET acknowledged = 1 WHERE delivery_id = ?",
                (delivery_id,),
            )
    return {"delivery_id": delivery_id, "acknowledged": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local synthetic SRE handoff delivery ledger.")
    parser.add_argument("--db", required=True, help="Signal DB path (never opened for writing)")
    parser.add_argument("--ledger", required=True, help="Separate local delivery ledger path")
    commands = parser.add_subparsers(dest="command", required=True)
    offer = commands.add_parser("offer", help="Store and return one synthetic handoff")
    offer.add_argument("--policy", required=True, help="JSON cost policy")
    offer.add_argument("--as-of", required=True, help="UTC cutoff (YYYY-MM-DDTHH:MM:SSZ)")
    commands.add_parser("pull", help="Read the oldest unacknowledged handoff")
    ack = commands.add_parser("ack", help="Record receipt of one exact handoff")
    ack.add_argument("delivery_id", help="64 lower-case hex delivery ID")
    args = parser.parse_args(argv)

    try:
        policy_path = args.policy if args.command == "offer" else None
        db, ledger = _separate_paths(args.db, args.ledger, policy_path)
        if args.command == "offer":
            result = _offer(db, ledger, policy_path, args.as_of)
        elif args.command == "pull":
            result = _pull(ledger)
        else:
            result = _ack(ledger, args.delivery_id)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
