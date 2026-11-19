"""Exercise reconciliation against a labeled, deterministic workload."""
import argparse
import csv
import io
import json
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter

from recon import BANK_FIELDS, PAYOUT_FIELDS, import_csv, initialize, state


def to_csv(header, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue()


def workload(size):
    banks, payouts, truth = [], [], {}
    for index in range(size):
        day = (date(2026, 1, 1) + timedelta(days=index % 28)).isoformat()
        payout = f"PO-{index:05}"
        bank = f"BK-{index:05}"
        cents = 12_500 + index * 25
        net = f"{cents // 100}.{cents % 100:02}"
        sales = f"{cents // 100 + 10}.{cents % 100:02}"
        payouts.append((payout, day, "CAD", sales, "8", "2", "0"))
        pattern = index % 10
        if pattern <= 6:
            banks.append((bank, day, "CAD", net, payout if pattern < 6 else "", "Deposit"))
            truth[payout] = ("matched", bank)
        elif pattern == 7:
            for suffix in ("A", "B"):
                banks.append((f"{bank}-{suffix}", day, "CAD", net, "", "Deposit"))
            truth[payout] = ("review", None)
        elif pattern == 8:
            banks.append((bank, day, "CAD", f"{cents // 100 + 1}.{cents % 100:02}", payout, "Mismatch"))
            truth[payout] = ("review", None)
        else:
            truth[payout] = ("waiting", None)
    return to_csv(BANK_FIELDS, banks), to_csv(PAYOUT_FIELDS, payouts), truth


def elapsed_ms(start):
    return round((perf_counter() - start) * 1000, 1)


def run(size, folder):
    bank, payouts, truth = workload(size)
    path = folder / "benchmark.sqlite"
    initialize(path)
    csv_path = folder / "crash-input.csv"
    csv_path.write_text(bank, encoding="utf-8")
    crash = (
        "import os,sys; from pathlib import Path; from recon import import_csv; "
        "import_csv(sys.argv[1],'bank','bank.csv',Path(sys.argv[2]).read_text(),"
        "after_row=lambda n: os._exit(73) if n == 50 else None)"
    )
    result = subprocess.run([sys.executable, "-B", "-c", crash, str(path), str(csv_path)], check=False)
    if result.returncode != 73:
        raise RuntimeError("the interrupted import did not reach the crash point")
    rows_after_crash = state(path)["bank_count"]
    start = perf_counter()
    imported_bank = import_csv(path, "bank", "bank.csv", bank)
    recovery_ms = elapsed_ms(start)
    start = perf_counter()
    imported_payouts = import_csv(path, "payouts", "payouts.csv", payouts)
    payout_ms = elapsed_ms(start)
    start = perf_counter()
    result = state(path)
    snapshot_ms = elapsed_ms(start)
    observed = {
        row["payout"]["id"]: (row["status"], row["bank"]["id"] if row["bank"] else None)
        for row in result["payouts"]
    }
    incorrect = sum(observed.get(key) != expected for key, expected in truth.items())
    paired = [item[1] for item in observed.values() if item[1] is not None]
    repeated = sum(
        import_csv(path, kind, f"{kind}.csv", content)["duplicates"]
        for kind, content in (("bank", bank), ("payouts", payouts))
    )
    assert rows_after_crash == 0
    assert incorrect == 0
    assert len(set(paired)) == len(paired)
    assert repeated == imported_bank["added"] + imported_payouts["added"]
    return {
        "bank_rows": imported_bank["added"], "payout_rows": imported_payouts["added"],
        "outcomes": dict(Counter(item[0] for item in observed.values())),
        "incorrect_outcomes": incorrect, "duplicate_rows_skipped_on_retry": repeated,
        "rows_committed_by_interrupted_import": rows_after_crash,
        "bank_reimport_after_crash_ms": recovery_ms, "payout_import_ms": payout_ms,
        "reconciliation_snapshot_ms": snapshot_ms,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payouts", type=int, default=5000)
    args = parser.parse_args()
    if not 100 <= args.payouts <= 10_000:
        parser.error("--payouts must be between 100 and 10000")
    with tempfile.TemporaryDirectory() as folder:
        print(json.dumps(run(args.payouts, Path(folder)), indent=2))


if __name__ == "__main__":
    main()
