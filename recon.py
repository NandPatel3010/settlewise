"""CSV validation, atomic imports, and conservative payout matching."""
import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BANK_FIELDS = ("bank_id", "posted_on", "currency", "amount", "payout_reference", "description")
PAYOUT_FIELDS = (
    "payout_id", "expected_on", "currency", "sales", "refunds", "fees", "adjustments"
)
FIELDS = {"bank": BANK_FIELDS, "payouts": PAYOUT_FIELDS}
CURRENCIES = {"CAD", "USD"}
MAX_ROWS = 10_000
WINDOW_DAYS = 3
CANDIDATE_PREVIEW = 24
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
MONEY = re.compile(r"-?[0-9]{1,8}(?:\.[0-9]{1,2})?")
SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
    digest TEXT NOT NULL, total INTEGER NOT NULL, added INTEGER NOT NULL,
    duplicates INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id), PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS decisions (
    payout_id TEXT PRIMARY KEY, bank_id TEXT NOT NULL UNIQUE,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY, payout_id TEXT NOT NULL, bank_id TEXT NOT NULL,
    action TEXT NOT NULL, occurred_at TEXT NOT NULL
);
"""


class InputError(ValueError):
    pass


class ConflictError(InputError):
    pass


@contextmanager
def database(path, *, write=False):
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with database(path, write=True) as db:
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                db.execute(statement)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def numeric(value):
    if not MONEY.fullmatch(value):
        raise InputError("amounts must be decimal numbers with at most two decimal places")
    return int(Decimal(value) * 100)


def parse_row(kind, row):
    fields = FIELDS[kind]
    identifier = row[fields[0]]
    if not IDENTIFIER.fullmatch(identifier):
        raise InputError(f"{fields[0]} must be a stable identifier, up to 64 characters")
    day = row[fields[1]]
    parsed_day = date.fromisoformat(day)
    if parsed_day.isoformat() != day or not 1900 <= parsed_day.year <= 2100:
        raise InputError(f"{fields[1]} must use YYYY-MM-DD between 1900 and 2100")
    currency = row["currency"].upper()
    if currency not in CURRENCIES:
        raise InputError("currency must be CAD or USD")
    data = {"id": identifier, "date": day, "currency": currency}

    if kind == "bank":
        reference = row["payout_reference"]
        if reference and not IDENTIFIER.fullmatch(reference):
            raise InputError("payout_reference must be blank or an exact payout ID")
        amount = numeric(row["amount"])
        if amount <= 0:
            raise InputError("bank files must contain positive deposits only")
        description = row["description"]
        if len(description) > 200 or any(ord(value) < 32 for value in description):
            raise InputError("description must be one line, up to 200 characters")
        return dict(data, amount=amount, reference=reference, description=description)

    breakdown = {key: numeric(row[key]) for key in fields[3:]}
    if min(breakdown["sales"], breakdown["refunds"], breakdown["fees"]) < 0:
        raise InputError("sales, refunds, and fees must be nonnegative amounts")
    net = breakdown["sales"] - breakdown["refunds"] - breakdown["fees"] + breakdown["adjustments"]
    if net <= 0:
        raise InputError("a payout's net amount must be positive")
    return dict(data, amount=net, **breakdown)


def parse_csv(kind, content):
    if kind not in FIELDS or not isinstance(content, str):
        raise InputError("choose a bank statement or payout report CSV")
    reader = csv.DictReader(io.StringIO(content.removeprefix("\ufeff"), newline=""), strict=True)
    rows = []
    try:
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or set(fields) != set(FIELDS[kind]):
            raise InputError("expected CSV columns: " + ", ".join(FIELDS[kind]))
        for row in reader:
            if len(rows) == MAX_ROWS:
                raise InputError(f"upload at most {MAX_ROWS:,} rows per file")
            if None in row or any(value is None for value in row.values()):
                raise InputError(f"line {reader.line_num}: wrong number of columns")
            try:
                rows.append(parse_row(kind, {key: value.strip() for key, value in row.items()}))
            except (ValueError, OverflowError) as error:
                raise InputError(f"line {reader.line_num}: {error}") from error
    except csv.Error as error:
        raise InputError(f"line {reader.line_num}: invalid CSV ({error})") from error
    if not rows:
        raise InputError("the CSV has no data rows")
    return rows


def import_csv(path, kind, filename, content, *, preview=False, after_row=None):
    """A retry may create an audit entry, but never a duplicate ledger row."""
    rows = parse_csv(kind, content)
    filename = str(filename).replace("\\", "/").rsplit("/", 1)[-1][:120] or "upload.csv"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    created_at = now()
    with database(path, write=not preview) as db:
        batch = None
        if not preview:
            batch = db.execute(
                "INSERT INTO batches(kind,name,digest,total,added,duplicates,created_at) "
                "VALUES(?,?,?,?,0,0,?)", (kind, filename, digest, len(rows), created_at)
            ).lastrowid

        seen = {}
        added = 0
        for row in rows:
            identifier = row["id"]
            payload = json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            existing = seen.get(identifier)
            if existing is None:
                stored = db.execute(
                    "SELECT payload FROM records WHERE kind=? AND id=?", (kind, identifier)
                ).fetchone()
                if stored:
                    existing = stored["payload"]
            if existing is not None and existing != payload:
                raise ConflictError(
                    f"{identifier} already has different data. No rows from this file were imported."
                )
            if existing is None:
                added += 1
                if not preview:
                    db.execute("INSERT INTO records VALUES(?,?,?,?)", (kind, identifier, payload, batch))
                    if after_row is not None:
                        after_row(added)
            seen[identifier] = payload

        result = {
            "name": filename, "total": len(rows), "added": added,
            "duplicates": len(rows) - added, "id": batch, "preview": rows[:5],
        }
        if not preview:
            db.execute(
                "UPDATE batches SET added=?, duplicates=? WHERE id=?",
                (result["added"], result["duplicates"], batch),
            )
        return result


def read_records(db):
    records = {"bank": {}, "payouts": {}}
    for entry in db.execute("SELECT kind,id,payload FROM records ORDER BY id"):
        records[entry["kind"]][entry["id"]] = json.loads(entry["payload"])
    decisions = {
        row["payout_id"]: row["bank_id"]
        for row in db.execute("SELECT payout_id, bank_id FROM decisions")
    }
    return records["bank"], records["payouts"], decisions


def reconcile(banks, payouts, decisions):
    available = set(banks) - set(decisions.values())
    referenced = defaultdict(list)
    by_amount_day = defaultdict(list)
    for bank_id in sorted(available):
        bank = banks[bank_id]
        if bank["reference"]:
            referenced[bank["reference"]].append(bank_id)
        else:
            by_amount_day[bank["currency"], bank["amount"], bank["date"]].append(bank_id)

    options = {}
    for payout_id, payout in payouts.items():
        if payout_id in decisions:
            continue
        refs = referenced.get(payout_id, [])
        if refs:
            eligible = [
                bank_id for bank_id in refs
                if same_value(banks[bank_id], payout)
            ]
            # Every explicitly referenced deposit is evidence, including mismatches.
            options[payout_id] = (len(refs), refs[:CANDIDATE_PREVIEW], len(eligible) != len(refs))
            continue
        day = date.fromisoformat(payout["date"])
        # Count by day instead of materializing every payout/deposit pair.
        keys = [
            (payout["currency"], payout["amount"], (day + timedelta(days=offset)).isoformat())
            for offset in range(-WINDOW_DAYS, WINDOW_DAYS + 1)
        ]
        candidate_count, preview = 0, []
        for key in keys:
            group = by_amount_day.get(key)
            if group:
                candidate_count += len(group)
                preview.extend(group[:CANDIDATE_PREVIEW - len(preview)])
        options[payout_id] = (candidate_count, preview, False)

    output = []
    for payout_id, payout in payouts.items():
        refs = referenced.get(payout_id, [])
        candidate_count, candidates, mismatch = options.get(payout_id, (0, [], False))
        bank_id = decisions.get(payout_id)
        reason = "Confirmed in review."
        method = "manual" if bank_id else None
        if not bank_id and refs and candidate_count == 1 and not mismatch:
            bank_id = candidates[0]
            method = "reference"
            reason = "Exact payout reference, amount and currency."

        if bank_id:
            status = "matched"
        elif candidate_count:
            status = "review"
            if mismatch:
                reason = "Referenced deposits disagree on amount or currency."
            elif refs:
                reason = "Multiple deposits carry this payout reference. Review before confirming."
            else:
                reason = "Amount and date suggest a possible match. Verify the deposit before confirming."
        else:
            status = "waiting"
            reason = "No matching deposit in the imported statement."

        output.append({
            "payout": payout, "bank": banks.get(bank_id), "status": status, "method": method,
            "reason": reason, "candidate_count": candidate_count if not bank_id else 0,
            "candidates": [banks[key] for key in candidates] if not bank_id else [],
        })
    output.sort(key=lambda row: (row["payout"]["date"], row["payout"]["id"]), reverse=True)
    used = {row["bank"]["id"] for row in output if row["bank"]}
    return output, sorted(
        (bank for key, bank in banks.items() if key not in used),
        key=lambda row: (row["date"], row["id"]), reverse=True,
    )


def same_value(bank, payout):
    return bank["amount"] == payout["amount"] and bank["currency"] == payout["currency"]


def state(path):
    with database(path) as db:
        banks, payouts, decisions = read_records(db)
        rows, unmatched = reconcile(banks, payouts, decisions)
        batches = [dict(row) for row in db.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 100")]
        reviews = [dict(row) for row in db.execute("SELECT * FROM review_log ORDER BY id DESC LIMIT 100")]
    return {
        "payouts": rows, "unmatched": unmatched, "imports": batches,
        "decisions": reviews, "bank_count": len(banks),
    }


def confirm(path, payout_id, bank_id):
    with database(path, write=True) as db:
        banks, payouts, decisions = read_records(db)
        if decisions.get(payout_id) == bank_id and payout_id in decisions:
            return
        rows, _ = reconcile(banks, payouts, decisions)
        target = next((row for row in rows if row["payout"]["id"] == payout_id), None)
        if not target or target["status"] != "review":
            raise ConflictError("This payout is no longer available for manual review. Refresh and try again.")
        payout = target["payout"]
        bank = banks.get(bank_id)
        has_reference = any(row["reference"] == payout_id for row in target["candidates"])
        valid_reference = bank and bank["reference"] == payout_id
        in_window = bank and abs(
            (date.fromisoformat(bank["date"]) - date.fromisoformat(payout["date"])).days
        ) <= WINDOW_DAYS
        if (
            not bank or bank_id in decisions.values() or not same_value(bank, payout)
            or not (valid_reference or (not has_reference and not bank["reference"] and in_window))
        ):
            raise ConflictError("Choose a current candidate with the same amount and currency.")
        timestamp = now()
        db.execute("INSERT INTO decisions VALUES(?,?,?)", (payout_id, bank_id, timestamp))
        db.execute(
            "INSERT INTO review_log(payout_id,bank_id,action,occurred_at) VALUES(?,?,?,?)",
            (payout_id, bank_id, "confirmed", timestamp),
        )


def reopen(path, payout_id, bank_id):
    with database(path, write=True) as db:
        result = db.execute(
            "DELETE FROM decisions WHERE payout_id=? AND bank_id=?", (payout_id, bank_id)
        )
        if result.rowcount:
            db.execute(
                "INSERT INTO review_log(payout_id,bank_id,action,occurred_at) VALUES(?,?,?,?)",
                (payout_id, bank_id, "reopened", now()),
            )
