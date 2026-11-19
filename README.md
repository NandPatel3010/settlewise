# SettleWise

A local workspace for tying payment-processor payouts to bank deposits. Open a payout to see its sales, refunds, fees, and adjustments. Review ambiguous matches and amount discrepancies before counting them as reconciled.

## Run it

Requires Python 3.10 or newer and a current browser. There are no packages to install.

**Windows:** Extract the ZIP and double-click `Start-SettleWise.cmd`, or run:

```powershell
py -3 app.py --open
```

**macOS / Linux:**

```sh
python3 app.py --open
```

The dashboard opens at `http://127.0.0.1:8765`. Keep the terminal window open while using the app. Use `--port 8766` if that port is occupied or `--data-dir PATH` to choose where the databases live.

## Try the demo

1. Open **PO-1029** in the payout ledger. Its $250.00 in sales, $22.00 in refunds, and $8.00 in fees explain the $220.00 deposit.
2. Open **Review queue**. **PO-1036** has two plausible bank rows, and **PO-1034** has a referenced deposit with the wrong amount. Confirm a candidate for PO-1036 and undo it from the payout detail to see both actions in the audit history.
3. Return to **Overview** and click **Import late payout**. The $328.65 deposit **BK-1031** becomes reconciled. Click **Retry late payout** and confirm that the import log records one skipped duplicate and the totals stay the same.
4. Switch to **My ledger** to work with a separate database. Use the sample files as templates. Preview validates a file, shows a sample of the normalized rows, and reports how many would be new. Import revalidates inside the write operation.

Export CSV downloads the full reconciliation report for the selected currency. Open **Review queue** to see unmatched bank deposits separately. The chart shows matched and still-open payout amounts by expected settlement day.

## CSV formats

Use UTF-8 files with a header row, one account and one processor per workspace. The supplied examples contain synthetic data. These normalized templates are project-specific and can be filled using exports from your bank and payment processor.

| File | Columns |
| --- | --- |
| Statement | `bank_id,posted_on,currency,amount,payout_reference,description` |
| Payout report | `payout_id,expected_on,currency,sales,refunds,fees,adjustments` |

Use stable, source-specific IDs and ISO dates (`YYYY-MM-DD`). Deposit amounts must be positive. Sales, refunds, and fees are nonnegative. Adjustments may be signed. All amounts use decimal notation with a dot and at most two fractional digits. CAD and USD are reconciled independently. Leave the payout reference blank when the statement has no exact payout ID. Split files above 10,000 rows or 1.8 MB.

## Matching and recovery rules

1. An exact payout reference can match only one bank row and must agree on both amount and currency. A discrepancy or multiple deposits with the same reference goes to review.
2. For deposits without a reference, the same amount and currency must be unique on both sides within three calendar days of the expected date. Later imports can change an automatic match if they create an ambiguity.
3. A manual confirmation reserves one deposit for one payout. Confirmations and undo actions are recorded in the same database transaction that changes the decision.
4. Import batches and new rows commit together. Repeated rows are identified by source ID and normalized data. If any known ID arrives with conflicting data, the whole batch rolls back. A process crash before commit leaves the file safe to retry.

Money is stored as integer cents. Reconciliation is computed from bank records, payouts, and manual decisions in one consistent SQLite snapshot. Amount/date lookups are indexed in memory by currency, amount, and day. Candidate counts are computed by bucket, and the detail view shows the first 24 candidates, with confirmation by ID available for the rest.

The app binds to loopback and keeps entries under `data/` by default. It uses local CSV files and local SQLite databases. It is a single-user portfolio application. The current scope covers individual deposits, normalized inputs, and conservative review.

## Verification

```sh
python -m unittest -v
python benchmark.py --payouts 5000
```

A repeatable synthetic run used 5,000 bank rows and 5,000 payouts. It included exact references, unique amount/date matches, duplicate-looking deposits, known discrepancies, and missing deposits.

| Result | Observed |
| --- | ---: |
| Correctly matched payouts | 3,500 |
| Payouts sent to review | 1,000 |
| Payouts awaiting a deposit | 500 |
| Incorrect outcomes against labeled fixtures | 0 |
| Rows skipped when both files were reimported | 10,000 |
| Rows left when the import process exited before commit | 0 |
| Bank reimport after the crash, single local run | 78.5 ms |
| Reconciliation snapshot, single local run | 60.1 ms |

The 14 tests cover matching, API request handling, process termination mid-import, overlapping retries, row conflicts, and the review workflow. Benchmark runtimes depend on the machine and workload.

## Source layout

- `recon.py` validates uploads, manages imports and review decisions, and computes matches.
- `app.py` serves the dashboard and local JSON API.
- `web/` contains the dashboard, chart, import flow, and review UI.
- `samples/` contains the sample ledger and late-payout CSV.
- `test_recon.py` and `benchmark.py` run verification from the command line.

[Stripe payout reconciliation workflow](https://docs.stripe.com/reports/payout-reconciliation) · [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
