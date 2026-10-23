"""Isolated API checks for cheque status and customer-ledger postings."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server


class QuietHandler(server.Handler):
    def log_message(self, fmt, *args):
        pass


class ChequeAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "isolated-demo.sqlite"
        server.initialize()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        with server.connect() as db:
            self.customer_id = db.execute("SELECT id FROM customers WHERE code='C-102'").fetchone()[0]
            self.cheque_id = db.execute("SELECT id FROM cheques WHERE cheque_no='DEMO-CHQ-1003'").fetchone()[0]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()
        server.DB_PATH = self.original_db_path

    def post(self, cheque_id, action, **fields):
        request = Request(
            f"{self.base_url}/api/cheques/{cheque_id}/events",
            data=json.dumps({"action": action, **fields}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def get_cheque(self, cheque_id):
        with server.connect() as db:
            return db.execute("SELECT status FROM cheques WHERE id=?", (cheque_id,)).fetchone()["status"]

    def get_json(self, path):
        with urlopen(f"{self.base_url}{path}") as response:
            return response.status, json.loads(response.read())

    def test_clearing_moves_cheque_asset_to_bank_once(self):
        for action in ("deposit", "present"):
            status, body = self.post(self.cheque_id, action)
            self.assertEqual(status, 200, body)
        status, body = self.post(self.cheque_id, "clear")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.get_cheque(self.cheque_id), "cleared")
        self.assertTrue(body["voucher_no"].startswith("CHQ-"))
        with server.connect() as db:
            posted = db.execute("SELECT id FROM journal_headers WHERE source_type='ChequeClear' AND source_id=? AND status='posted'", (self.cheque_id,)).fetchone()
            self.assertIsNotNone(posted)
            amounts = db.execute("SELECT ROUND(SUM(debit),2),ROUND(SUM(credit),2) FROM journal_lines WHERE journal_id=?", (posted["id"],)).fetchone()
            self.assertEqual(tuple(amounts), (42000.0, 42000.0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM v_customer_statement_lines WHERE source_type='ChequeClear' AND customer_id=?", (self.customer_id,)).fetchone()[0], 0)
        status, _ = self.post(self.cheque_id, "clear")
        self.assertEqual(status, 400)
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM journal_headers WHERE source_type='ChequeClear' AND source_id=?", (self.cheque_id,)).fetchone()[0], 1)

    def test_bounce_restores_customer_receivable_and_return_is_physical_only(self):
        for action in ("deposit", "present"):
            self.assertEqual(self.post(self.cheque_id, action)[0], 200)
        status, body = self.post(self.cheque_id, "bounce", reason="Fictional bank return")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.get_cheque(self.cheque_id), "bounced")
        with server.connect() as db:
            rows = db.execute("SELECT source_type,debit,credit FROM v_customer_statement_lines WHERE customer_id=? AND source_type='ChequeBounce'", (self.customer_id,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["debit"], rows[0]["credit"]), (42000.0, 0))
        status, _ = self.post(self.cheque_id, "physical_return", reason="Returned to customer")
        self.assertEqual(status, 200)
        self.assertEqual(self.get_cheque(self.cheque_id), "returned")
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM journal_headers WHERE source_type='ChequeBounce' AND source_id=?", (self.cheque_id,)).fetchone()[0], 1)

    def test_customer_return_reverses_posted_receipt(self):
        status, body = self.post(self.cheque_id, "return", reason="Returned to customer")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.get_cheque(self.cheque_id), "returned")
        with server.connect() as db:
            reversal = db.execute("SELECT id FROM journal_headers WHERE source_type='ChequeReturn' AND source_id=? AND status='posted'", (self.cheque_id,)).fetchone()
            self.assertIsNotNone(reversal)
            rows = db.execute("SELECT debit,credit FROM v_customer_statement_lines WHERE customer_id=? AND source_type='ChequeReturn'", (self.customer_id,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["debit"], rows[0]["credit"]), (42000.0, 0))

    def test_profit_loss_comparison_returns_period_amounts_and_delta(self):
        status, report = self.get_json("/api/reports/profit-loss-comparison?from1=2026-09-01&to1=2026-09-30&from2=2026-10-01&to2=2026-10-31")
        self.assertEqual(status, 200)
        sales = next(row for row in report["rows"] if row["label"] == "Sales")
        net = next(row for row in report["rows"] if row["label"] == "Net Profit")
        self.assertEqual((sales["range_1"], sales["range_2"], sales["difference"]), (0.0, 180000.0, 180000.0))
        self.assertEqual((net["range_1"], net["range_2"], net["difference"]), (0.0, 180000.0, 180000.0))
        request = Request(f"{self.base_url}/api/reports/profit-loss-comparison?from1=2026-10-01&to1=2026-09-30&from2=2026-10-01&to2=2026-10-31")
        try:
            urlopen(request)
            self.fail("Reversed date ranges should be rejected.")
        except HTTPError as error:
            self.assertEqual(error.code, 400)

    def test_wise_profit_loss_groups_posted_entries_by_selected_dimension(self):
        with server.connect() as db:
            org = db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
            income = db.execute("SELECT id FROM accounts WHERE code='4000'").fetchone()[0]
            station = db.execute("SELECT id FROM stations WHERE code='HQ'").fetchone()[0]
            cost_center = db.execute("SELECT id FROM cost_centers WHERE code='GEN'").fetchone()[0]
            db.execute("INSERT INTO journal_headers(organization_id,voucher_no,voucher_date,source_type,source_id,status,created_by) VALUES (?, 'FICTION-WISE-1','2026-10-08','FictionalReportCheck',90,'posted',1)", (org,))
            journal_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,station_id,cost_center_id,description,credit) VALUES (?,?,?,?,?,'Fictional tagged income',25000)", (journal_id,1,income,station,cost_center))
        status, report = self.get_json("/api/reports/wise-profit-and-loss?from=2026-10-01&to=2026-10-31&mode=station")
        self.assertEqual(status, 200, report)
        self.assertEqual(report['mode'], 'station')
        self.assertIn('Main Office', [column['label'] for column in report['columns']])
        sales = next(row for row in report['rows'] if row['key'] == 'sales')
        hq_column = next(column for column in report['columns'] if column['label'] == 'Main Office')
        self.assertEqual(sales['values'][hq_column['key']], 25000.0)
        detail = next(row for row in report['rows'] if row['kind'] == 'account' and row['account'] == 'Service Income')
        self.assertEqual(detail['values'][hq_column['key']], 25000.0)
        total = next(row for row in report['rows'] if row['key'] == 'net_profit')
        self.assertEqual(total['values'][hq_column['key']], 25000.0)
        status, cost_report = self.get_json("/api/reports/wise-profit-and-loss?from=2026-10-01&to=2026-10-31&mode=cost_center")
        self.assertEqual(status, 200, cost_report)
        self.assertEqual(cost_report['column_dimension'], 'Cost Center')
        general = next(column for column in cost_report['columns'] if column['label'] == 'General')
        self.assertEqual(next(row for row in cost_report['rows'] if row['key'] == 'sales')['values'][general['key']], 25000.0)

    def test_wise_profit_loss_rejects_reversed_dates_and_unknown_mode(self):
        for query in ('from=2026-10-31&to=2026-10-01&mode=station', 'from=2026-10-01&to=2026-10-31&mode=other'):
            try:
                urlopen(f"{self.base_url}/api/reports/wise-profit-and-loss?{query}")
                self.fail('Invalid wise report parameters should be rejected.')
            except HTTPError as error:
                self.assertEqual(error.code, 400)

    def test_ledger_currency_filter_keeps_currency_amounts_and_balances_in_scope(self):
        with server.connect() as db:
            cash = db.execute("SELECT id FROM accounts WHERE code='1100'").fetchone()[0]
            db.execute("UPDATE journal_headers SET exchange_rate=2 WHERE source_type='CollectionReceipt' AND source_id=(SELECT id FROM receipt_headers WHERE receipt_date='2026-10-15')")
        status, report = self.get_json(f"/api/reports/ledger?detail_account_id={cash}&from=2026-10-01&to=2026-10-31&currency=USD")
        self.assertEqual(status, 200, report)
        self.assertEqual(report['currency'], 'USD')
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['rows'][0]['currency'], 'USD')
        self.assertEqual(report['totals']['debit'], 1100.0)

    def test_collection_history_filters_demo_receipts_and_totals_deposit_lines(self):
        status, report = self.get_json("/api/reports/collection-history?from=2026-10-01&to=2026-10-31&all_detail_accounts=1")
        self.assertEqual(status, 200, report)
        self.assertEqual(len(report['rows']), 5)
        self.assertEqual(report['totals']['amount'], 350700.0)
        status, filtered = self.get_json(f"/api/reports/collection-history?from=2026-10-01&to=2026-10-31&customer_id={self.customer_id}")
        self.assertEqual(status, 200, filtered)
        self.assertEqual(len(filtered['rows']), 1)
        self.assertEqual(filtered['rows'][0]['instrument_no'], 'DEMO-CHQ-1003')
        self.assertEqual(filtered['rows'][0]['cheque_bank'], 'Sample Bank')
        self.assertEqual(filtered['totals']['amount'], 42000.0)

    def test_replacement_reapplies_receivable_then_can_clear(self):
        for action in ("deposit", "present", "bounce"):
            status, _ = self.post(self.cheque_id, action, reason="Fictional return" if action == "bounce" else "")
            self.assertEqual(status, 200)
        status, replacement = self.post(self.cheque_id, "replace", cheque_no="DEMO-CHQ-REPL-1", cheque_date="2026-11-25", payer_bank="Sample Bank")
        self.assertEqual(status, 201, replacement)
        replacement_id = replacement["id"]
        self.assertIsNotNone(replacement["voucher_no"])
        with server.connect() as db:
            rows = db.execute("SELECT source_type,debit,credit FROM v_customer_statement_lines WHERE customer_id=? AND source_type='ChequeReplacement'", (self.customer_id,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["debit"], rows[0]["credit"]), (0, 42000.0))
        for action in ("deposit", "present", "clear"):
            status, body = self.post(replacement_id, action)
            self.assertEqual(status, 200, body)
        self.assertEqual(self.get_cheque(replacement_id), "cleared")
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM journal_headers WHERE source_type='ChequeReplacement' AND source_id=?", (replacement_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM journal_headers WHERE source_type='ChequeClear' AND source_id=?", (replacement_id,)).fetchone()[0], 1)
            cheque_asset = db.execute("SELECT ROUND(SUM(l.debit-l.credit),2) FROM journal_lines l JOIN accounts a ON a.id=l.account_id WHERE a.code='1120'").fetchone()[0]
            bank_asset = db.execute("SELECT ROUND(SUM(l.debit-l.credit),2) FROM journal_lines l JOIN accounts a ON a.id=l.account_id WHERE a.code='1110'").fetchone()[0]
            self.assertEqual(cheque_asset, 0.0)
            self.assertEqual(bank_asset, 42000.0)


if __name__ == "__main__":
    unittest.main()
