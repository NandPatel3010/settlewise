import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from app import Application, Handler, ROOT
from recon import ConflictError, InputError, confirm, import_csv, initialize, reopen, state

BANK = "bank_id,posted_on,currency,amount,payout_reference,description\n"
PAYOUT = "payout_id,expected_on,currency,sales,refunds,fees,adjustments\n"


class FakeConnection:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, *_args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, _seconds):
        pass


class QuietHandler(Handler):
    def log_message(self, *_args):
        pass


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "ledger.sqlite"
        initialize(self.path)

    def take(self, kind, *rows, **kwargs):
        header = BANK if kind == "bank" else PAYOUT
        return import_csv(self.path, kind, "fixture.csv", header + "\n".join(rows) + "\n", **kwargs)

    def test_exact_reference_and_breakdown_use_integer_cents(self):
        self.take("bank", "B-1,2026-11-08,CAD,220.00,P-1,Deposit")
        self.take("payouts", "P-1,2026-11-07,CAD,250.00,22.00,8.00,0")
        row = state(self.path)["payouts"][0]
        self.assertEqual((row["method"], row["payout"]["amount"]), ("reference", 22000))
        self.assertEqual(row["payout"]["refunds"], 2200)
        self.assertEqual(row["bank"]["id"], "B-1")

    def test_preview_never_writes_and_retry_skips_duplicate(self):
        line = "B-1,2026-11-08,CAD,220.00,,Deposit"
        preview = self.take("bank", line, line, preview=True)
        self.assertEqual((preview["added"], preview["duplicates"], preview["id"]), (1, 1, None))
        self.assertEqual(state(self.path)["imports"], [])
        self.take("bank", line)
        retry = self.take("bank", line.replace("220.00", "220"))
        self.assertEqual((retry["added"], retry["duplicates"]), (0, 1))
        self.assertEqual(state(self.path)["bank_count"], 1)

    def test_conflicting_row_rolls_back_entire_batch_and_audit(self):
        self.take("bank", "B-1,2026-11-08,CAD,220.00,,Deposit")
        with self.assertRaises(ConflictError):
            self.take(
                "bank", "B-2,2026-11-08,CAD,350.00,,New", "B-1,2026-11-08,CAD,21.00,,Conflict"
            )
        ledger = state(self.path)
        self.assertEqual(ledger["bank_count"], 1)
        self.assertEqual(len(ledger["imports"]), 1)

    def test_invalid_csv_reports_line_and_writes_nothing(self):
        with self.assertRaisesRegex(InputError, "line 2"):
            self.take("bank", "B-1,2026-11-08,CAD,20.005,,Deposit")
        with self.assertRaisesRegex(InputError, "expected CSV columns"):
            import_csv(self.path, "bank", "wrong.csv", "id,amount\n1,22\n")
        self.assertEqual(state(self.path)["bank_count"], 0)

    def test_mutual_uniqueness_prevents_double_assignment(self):
        self.take("bank", "B-1,2026-11-08,CAD,220.00,,Deposit")
        self.take(
            "payouts", "P-1,2026-11-07,CAD,220,0,0,0", "P-2,2026-11-08,CAD,220,0,0,0"
        )
        result = state(self.path)
        self.assertEqual([row["status"] for row in result["payouts"]], ["review", "review"])
        self.assertEqual(len(result["unmatched"]), 1)

    def test_confirm_and_reopen_are_idempotent_and_audited(self):
        self.take("bank", "B-1,2026-11-08,CAD,220,,One", "B-2,2026-11-08,CAD,220,,Two")
        self.take("payouts", "P-1,2026-11-07,CAD,220,0,0,0")
        confirm(self.path, "P-1", "B-2")
        confirm(self.path, "P-1", "B-2")
        self.assertEqual(state(self.path)["payouts"][0]["method"], "manual")
        with self.assertRaises(ConflictError):
            confirm(self.path, "P-1", "B-1")
        reopen(self.path, "P-1", "B-2")
        reopen(self.path, "P-1", "B-2")
        result = state(self.path)
        self.assertEqual(result["payouts"][0]["status"], "review")
        self.assertEqual([event["action"] for event in result["decisions"]], ["reopened", "confirmed"])

    def test_mismatched_reference_blocks_fallback(self):
        self.take("bank", "B-1,2026-11-08,CAD,221.00,P-1,Wrong", "B-2,2026-11-08,CAD,220.00,,Candidate")
        self.take("payouts", "P-1,2026-11-08,CAD,220,0,0,0")
        result = state(self.path)
        self.assertEqual(result["payouts"][0]["status"], "review")
        with self.assertRaises(ConflictError):
            confirm(self.path, "P-1", "B-1")
        self.assertEqual(len(result["unmatched"]), 2)

    def test_dense_collision_preview_is_bounded_and_all_candidates_are_reviewable(self):
        self.take("bank", *[f"B-{i:02},2026-11-08,CAD,220,,Deposit" for i in range(30)])
        self.take("payouts", "P-1,2026-11-08,CAD,220,0,0,0")
        row = state(self.path)["payouts"][0]
        self.assertEqual((row["candidate_count"], len(row["candidates"])), (30, 24))
        confirm(self.path, "P-1", "B-29")
        self.assertEqual(state(self.path)["payouts"][0]["bank"]["id"], "B-29")

    def test_auto_matching_respects_currency_and_date(self):
        self.take("bank", "B-1,2026-11-05,CAD,220,,Too early", "B-2,2026-11-09,USD,220,,USD")
        self.take("payouts", "P-1,2026-11-09,CAD,220,0,0,0")
        self.assertEqual(state(self.path)["payouts"][0]["status"], "waiting")
        self.take("bank", "B-3,2026-11-12,CAD,220,,Three days late")
        self.assertEqual(state(self.path)["payouts"][0]["bank"]["id"], "B-3")

    def test_late_report_matches_existing_deposit(self):
        self.take("bank", "B-1,2026-11-08,CAD,220,P-1,Deposit")
        self.assertEqual(len(state(self.path)["unmatched"]), 1)
        self.take("payouts", "P-1,2026-11-07,CAD,220,0,0,0")
        self.assertEqual(state(self.path)["unmatched"], [])

    def test_concurrent_retries_have_one_winner(self):
        content = BANK + "B-1,2026-11-08,CAD,220,,Deposit\n"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda _: import_csv(self.path, "bank", "retry.csv", content), range(4)
            ))
        self.assertEqual(sorted(row["added"] for row in results), [0, 0, 0, 1])
        self.assertEqual(state(self.path)["bank_count"], 1)

    def test_abrupt_process_exit_leaves_zero_rows_then_file_retries(self):
        file = ROOT / "samples" / "demo-bank.csv"
        script = (
            "import os,sys; from pathlib import Path; from recon import import_csv; "
            "import_csv(sys.argv[1],'bank','sample.csv',Path(sys.argv[2]).read_text(),"
            "after_row=lambda n: os._exit(73) if n == 2 else None)"
        )
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.path), str(file)],
                                cwd=ROOT, check=False)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(state(self.path)["bank_count"], 0)
        self.assertEqual(state(self.path)["imports"], [])
        replay = import_csv(self.path, "bank", "sample.csv", file.read_text())
        self.assertEqual(replay["added"], 11)

    def http(self, app, route, *, method="GET", payload=None, origin=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        request = f"{method} {route} HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n"
        if body:
            request += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        if origin:
            request += f"Origin: {origin}\r\n"
        sock = FakeConnection((request + "\r\n").encode() + body)
        QuietHandler(sock, ("127.0.0.1", 0), SimpleNamespace(app=app, server_port=8765))
        head, _, payload = bytes(sock.output).partition(b"\r\n\r\n")
        return head, payload

    def test_http_assets_and_demo_are_wired_and_isolated(self):
        app = Application(self.root / "http")
        head, payload = self.http(app, "/")
        self.assertIn(b"200 OK", head)
        self.assertIn(b"Content-Security-Policy", head)
        self.assertIn(b'id="content"', payload)
        head, payload = self.http(app, "/ui.js")
        self.assertIn(b"javascript", head)
        self.assertIn(b"function refresh", payload)
        _, demo = self.http(app, "/api/state?workspace=demo")
        _, local = self.http(app, "/api/state?workspace=local")
        self.assertEqual(len(json.loads(demo)["payouts"]), 10)
        self.assertEqual(json.loads(local)["bank_count"], 0)

    def test_http_import_preview_commit_and_cross_origin_rejection(self):
        app = Application(self.root / "http")
        payload = {"kind": "bank", "name": "deposit.csv", "content": BANK + "B-1,2026-11-08,CAD,220,,Local\n"}
        head, _ = self.http(app, "/api/import?workspace=local", method="POST", payload=payload,
                            origin="https://unrelated.test")
        self.assertIn(b"403 Forbidden", head)
        _, preview = self.http(app, "/api/preview?workspace=local", method="POST", payload=payload)
        self.assertIsNone(json.loads(preview)["id"])
        _, result = self.http(app, "/api/import?workspace=local", method="POST", payload=payload,
                              origin="http://127.0.0.1:8765")
        self.assertEqual(json.loads(result)["added"], 1)
        head, _ = self.http(app, "/api/demo-late?workspace=local", method="POST", payload={})
        self.assertIn(b"404 Not Found", head)


if __name__ == "__main__":
    unittest.main()
