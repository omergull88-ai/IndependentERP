"""Small, local-only SQLite API for the ERP replica's fictional demo data."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "demo_erp_replica.sqlite"
SCHEMA = ROOT / "database" / "schema.sql"


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def validate_receipt_invoice_allocations(db, organization_id, lines, currency_id):
    requested={}
    for line in lines:
        invoice_no=str(line['invoice_no'] or '').strip()
        if invoice_no:
            key=(int(line['customer_id']),invoice_no)
            requested[key]=round(requested.get(key,0)+float(line['gross_amount']),2)
    for (customer_id,invoice_no),amount in requested.items():
        invoice=db.execute("""SELECT j.id,SUM(l.debit-l.credit) AS amount FROM journal_headers j
            JOIN journal_lines l ON l.journal_id=j.id JOIN customers cu ON cu.id=l.customer_id
            WHERE j.organization_id=? AND j.status='posted' AND j.source_type IN ('SalesInvoice','Invoice')
              AND j.voucher_no=? AND j.currency_id=? AND cu.id=? AND l.account_id=cu.account_id AND l.debit>l.credit
            GROUP BY j.id""",(organization_id,invoice_no,currency_id,customer_id)).fetchone()
        if invoice is None: raise ValueError(f'Invoice {invoice_no} is not a posted invoice for this customer and currency.')
        allocated=db.execute("""SELECT COALESCE(SUM(l.gross_amount),0) FROM receipt_lines l
            JOIN receipt_headers h ON h.id=l.receipt_id
            WHERE h.organization_id=? AND h.status='posted' AND l.customer_id=? AND l.invoice_no=?""",(organization_id,customer_id,invoice_no)).fetchone()[0]
        outstanding=round(float(invoice['amount'])-float(allocated),2)
        if amount>outstanding: raise ValueError(f'Invoice {invoice_no} has only {outstanding:.2f} still outstanding.')


def validate_cheque_deposit_account(db, organization_id, method, account_id):
    """Keep received cheques in the asset account used by cheque lifecycle journals."""
    if method != 'cheque':
        return
    account=db.execute("""SELECT id FROM accounts WHERE id=? AND organization_id=?
        AND code='1120' AND account_type='asset' AND account_level=4 AND active=1""",
        (account_id,organization_id)).fetchone()
    if account is None:
        raise ValueError('Cheque deposits must use the active Cheques In Hand account (1120).')


def initialize():
    with connect() as db:
        has_schema=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='organizations'").fetchone()
        if has_schema is None:
            db.executescript(SCHEMA.read_text(encoding="utf-8"))
        db.execute("CREATE TABLE IF NOT EXISTS cost_center_heads (id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES organizations(id), code TEXT NOT NULL, name TEXT NOT NULL, UNIQUE(organization_id, code))")
        db.execute("CREATE TABLE IF NOT EXISTS bank_masters (id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES organizations(id), code TEXT NOT NULL, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), UNIQUE(organization_id, code))")
        cost_center_columns={row['name'] for row in db.execute("PRAGMA table_info(cost_centers)").fetchall()}
        if 'head_id' not in cost_center_columns:
            db.execute("ALTER TABLE cost_centers ADD COLUMN head_id INTEGER REFERENCES cost_center_heads(id)")
        deposit_columns={row['name'] for row in db.execute("PRAGMA table_info(deposit_splits)").fetchall()}
        if deposit_columns and 'customer_id' not in deposit_columns:
            db.execute("ALTER TABLE deposit_splits ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
        if deposit_columns and 'currency_date' not in deposit_columns:
            db.execute("ALTER TABLE deposit_splits ADD COLUMN currency_date TEXT")
        receipt_columns={row['name'] for row in db.execute("PRAGMA table_info(receipt_headers)").fetchall()}
        for name,definition in [('call_label',"TEXT NOT NULL DEFAULT ''"),('cheque_type',"TEXT NOT NULL DEFAULT ''"),('locker',"TEXT NOT NULL DEFAULT ''")]:
            if receipt_columns and name not in receipt_columns:
                db.execute(f"ALTER TABLE receipt_headers ADD COLUMN {name} {definition}")
        cheque_columns={row['name'] for row in db.execute("PRAGMA table_info(cheques)").fetchall()}
        for name,definition in [('currency_date','TEXT'),('signatory',"TEXT NOT NULL DEFAULT ''")]:
            if cheque_columns and name not in cheque_columns:
                db.execute(f"ALTER TABLE cheques ADD COLUMN {name} {definition}")
        if db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0:
            db.execute("INSERT INTO organizations(code,name) VALUES ('DEMO','Demo Company')")
            org = db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
            db.executemany("INSERT INTO bank_masters(organization_id,code,name) VALUES (?,?,?)", [(org,'DB-01','Demo Commercial Bank'),(org,'DB-02','Demo City Bank'),(org,'DB-03','Demo National Bank')])
            db.execute("INSERT INTO users(organization_id,username,display_name) VALUES (?, 'demo', 'Jordan Demo')", (org,))
            for code, name, units in [('PKR','Pakistani Rupee',2),('USD','US Dollar',2),('EUR','Euro',2),('GBP','Pound Sterling',2)]:
                db.execute("INSERT INTO currencies(code,name,minor_units) VALUES (?,?,?)", (code,name,units))
            for code,name,typ in [('1100','Cash on Hand','asset'),('1110','Bank Account','asset'),('1120','Cheques In Hand','asset'),('1200','Accounts Receivable','asset'),('4000','Service Income','income'),('4090','Discount Allowed','expense'),('4095','Discount Received','income')]:
                db.execute("INSERT INTO accounts(organization_id,code,name,account_type) VALUES (?,?,?,?)",(org,code,name,typ))
            db.execute("UPDATE accounts SET account_head_type='Customer' WHERE organization_id=? AND code='1200'",(org,))
            for code,name,typ in [('1','Assets','asset'),('2','Liabilities','liability'),('3','Equity','equity'),('4','Income','income'),('5','Expenses','expense')]:
                db.execute("INSERT INTO accounts(organization_id,code,name,account_type,account_level,is_control) VALUES (?,?,?,?,1,1)",(org,code,name,typ))
            roots={row['code']:row['id'] for row in db.execute("SELECT id,code FROM accounts WHERE organization_id=? AND account_level=1",(org,))}
            for code,name,typ,root_code in [('11','Current Assets','asset','1'),('21','Current Liabilities','liability','2'),('31','Equity Accounts','equity','3'),('41','Operating Income','income','4'),('51','Operating Expenses','expense','5')]:
                db.execute("INSERT INTO accounts(organization_id,code,name,account_type,parent_id,account_level,is_control) VALUES (?,?,?,?,?,2,1)",(org,code,name,typ,roots[root_code]))
            groups={row['code']:row['id'] for row in db.execute("SELECT id,code FROM accounts WHERE organization_id=? AND account_level=2",(org,))}
            for name,group_code in [('Cash and Bank','11'),('Customer Balances','11'),('Service Revenue','41'),('Operating Adjustments','51')]:
                db.execute("INSERT INTO accounts(organization_id,code,name,account_type,parent_id,account_level,is_control) VALUES (?,NULL,?,?,?,3,1)",(org,name,db.execute("SELECT account_type FROM accounts WHERE id=?",(groups[group_code],)).fetchone()[0],groups[group_code]))
            heads={row['name']:row['id'] for row in db.execute("SELECT id,name FROM accounts WHERE organization_id=? AND account_level=3",(org,))}
            for code,head_name in [('1100','Cash and Bank'),('1110','Cash and Bank'),('1120','Cash and Bank'),('1200','Customer Balances'),('4000','Service Revenue'),('4090','Operating Adjustments'),('4095','Service Revenue')]:
                db.execute("UPDATE accounts SET parent_id=? WHERE organization_id=? AND code=?",(heads[head_name],org,code))
            ar = db.execute("SELECT id FROM accounts WHERE code='1200'").fetchone()[0]
            for code,name in [('C-100','Northwind Traders'),('C-101','Contoso Services'),('C-102','Fabrikam Supplies'),('C-103','Adventure Works'),('C-104','Blue Yonder Retail')]:
                db.execute("INSERT INTO customers(organization_id,account_id,code,display_name) VALUES (?,?,?,?)",(org,ar,code,name))
            for code,name in [('HQ','Main Office'),('EB','East Branch'),('NB','North Branch')]:
                db.execute("INSERT INTO stations(organization_id,code,name) VALUES (?,?,?)",(org,code,name))
            for code,name in [('GEN','General'),('OPS','Operations')]:
                db.execute("INSERT INTO cost_centers(organization_id,code,name) VALUES (?,?,?)",(org,code,name))
            db.execute("INSERT INTO cost_center_heads(organization_id,code,name) VALUES (?, 'OPS', 'Operations')",(org,))
            cost_head_id=db.execute("SELECT id FROM cost_center_heads WHERE organization_id=? AND code='OPS'",(org,)).fetchone()[0]
            db.execute("UPDATE cost_centers SET head_id=? WHERE organization_id=?",(cost_head_id,org))
            for n,(customer,dt,gross,discount,status,currency) in enumerate([
                ('C-100','2026-10-19',125000,0,'posted','PKR'),('C-101','2026-10-18',84500,500,'approved','PKR'),('C-102','2026-10-17',42000,0,'posted','PKR'),('C-103','2026-10-15',2200,0,'posted','USD'),('C-104','2026-10-14',98700,1200,'approved','PKR')], start=37):
                rno = f'RCP-{n:05d}'
                currency_id=db.execute("SELECT id FROM currencies WHERE code=?",(currency,)).fetchone()[0]
                db.execute("INSERT INTO receipt_headers(organization_id,receipt_no,receipt_date,currency_id,exchange_rate,status,created_by) VALUES (?,?,?,?,?,?,1)",(org,rno,dt,currency_id,1,status))
                rid=db.execute("SELECT last_insert_rowid()").fetchone()[0]
                cid=db.execute("SELECT id FROM customers WHERE code=?",(customer,)).fetchone()[0]
                cheque=customer=='C-102'
                db.execute("INSERT INTO receipt_lines(receipt_id,line_no,customer_id,payment_type,gross_amount,discount_amount) VALUES (?,1,?,?,?,?)",(rid,cid,'cheque' if cheque else 'cash',gross,discount))
                account_id=db.execute("SELECT id FROM accounts WHERE code=?",('1120' if cheque else '1100',)).fetchone()[0]
                cheque_no='DEMO-CHQ-1003' if cheque else ''
                cheque_date='2026-11-20' if cheque else None
                bank='Sample Bank' if cheque else ''
                method='cheque' if cheque else 'cash'
                db.execute("INSERT INTO deposit_splits(receipt_id,line_no,account_id,method,bank_label,instrument_no,instrument_date,amount) VALUES (?,1,?,?,?,?,?,?)",(rid,account_id,method,bank,cheque_no,cheque_date,gross-discount))
                if cheque:
                    split_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    db.execute("INSERT INTO cheques(organization_id,receipt_id,deposit_split_id,cheque_no,cheque_date,payer_bank,customer_id,currency_id,currency_rate,amount,base_amount,status,locker,remarks) VALUES (?,?,?,?,?,?,?,?,?,?,?,'in_hand','Main Locker','Fictional seeded cheque')",(org,rid,split_id,cheque_no,cheque_date,bank,cid,currency_id,1,gross-discount,gross-discount))
                    cheque_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    db.execute("INSERT INTO cheque_events(cheque_id,event_type,from_status,to_status,actor_id) VALUES (?,'received',NULL,'in_hand',1)",(cheque_id,))
                if status=='posted':
                    voucher_no=f'VR-{n+7700:05d}'
                    db.execute("INSERT INTO journal_headers(organization_id,voucher_no,voucher_date,source_type,source_id,status,created_by,posted_at) VALUES (?,?,?,'CollectionReceipt',?,'posted',1,CURRENT_TIMESTAMP)",(org,voucher_no,dt,rid))
                    journal_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    ar_account=db.execute("SELECT id FROM accounts WHERE code='1200'").fetchone()[0]
                    net_amount=gross-discount
                    db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,description,debit) VALUES (?,1,?,'Receipt deposit',?)",(journal_id,account_id,net_amount))
                    db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,customer_id,description,credit) VALUES (?,2,?,?,'Customer receipt',?)",(journal_id,ar_account,cid,net_amount))
            # Fictional opening invoice keeps the customer statement example meaningful.
            sample_customer=db.execute("SELECT id FROM customers WHERE code='C-100'").fetchone()[0]
            ar_account=db.execute("SELECT id FROM accounts WHERE code='1200'").fetchone()[0]
            income_account=db.execute("SELECT id FROM accounts WHERE code='4000'").fetchone()[0]
            db.execute("INSERT INTO journal_headers(organization_id,voucher_no,voucher_date,source_type,source_id,status,created_by,posted_at) VALUES (?,'INV-DEMO-01','2026-10-01','SalesInvoice',1,'posted',1,CURRENT_TIMESTAMP)",(org,))
            opening_journal=db.execute('SELECT last_insert_rowid()').fetchone()[0]
            db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,customer_id,description,debit) VALUES (?,1,?,?,'Fictional opening invoice',180000)",(opening_journal,ar_account,sample_customer))
            db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,description,credit) VALUES (?,2,?,'Fictional service revenue',180000)",(opening_journal,income_account))
        org_row=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()
        if org_row:
            org=org_row['id']
            db.executemany("INSERT OR IGNORE INTO bank_masters(organization_id,code,name) VALUES (?,?,?)", [(org,'DB-01','Demo Commercial Bank'),(org,'DB-02','Demo City Bank'),(org,'DB-03','Demo National Bank')])
            db.execute("INSERT OR IGNORE INTO cost_center_heads(organization_id,code,name) VALUES (?, 'OPS', 'Operations')",(org,))
            cost_head_id=db.execute("SELECT id FROM cost_center_heads WHERE organization_id=? AND code='OPS'",(org,)).fetchone()[0]
            db.execute("UPDATE cost_centers SET head_id=? WHERE organization_id=? AND head_id IS NULL",(cost_head_id,org))


def json_default(value):
    if isinstance(value, sqlite3.Row): return dict(value)
    raise TypeError(type(value).__name__)


def post_cheque_lifecycle_journal(db, organization_id, cheque, action):
    """Post the accounting leg for a cleared, bounced, or bounced replacement cheque."""
    accounts={row['code']:row['id'] for row in db.execute(
        "SELECT code,id FROM accounts WHERE organization_id=? AND active=1 AND account_level=4 AND code IN ('1110','1120','1200')",
        (organization_id,)).fetchall()}
    required=('1110','1120') if action=='clear' else ('1120','1200')
    if any(code not in accounts for code in required):
        raise ValueError('The demo chart needs active bank, cheques-in-hand and receivable accounts before this action can post.')
    split=db.execute("SELECT station_id,cost_center_id FROM deposit_splits WHERE id=? AND receipt_id=?",
                     (cheque['deposit_split_id'],cheque['receipt_id'])).fetchone()
    if split is None: raise ValueError('The cheque has no valid receipt deposit split.')
    if action=='clear':
        lines=[(accounts['1110'],None,split['station_id'],split['cost_center_id'],'Cheque cleared to bank',cheque['base_amount'],0,cheque['amount'],0),
               (accounts['1120'],None,split['station_id'],split['cost_center_id'],'Cheque cleared from cheques in hand',0,cheque['base_amount'],0,cheque['amount'])]
        source_type='ChequeClear'
    elif action in ('bounce','return'):
        lines=[(accounts['1200'],cheque['customer_id'],split['station_id'],split['cost_center_id'],'Bounced cheque restored to customer receivable',cheque['base_amount'],0,cheque['amount'],0),
               (accounts['1120'],None,split['station_id'],split['cost_center_id'],'Bounced cheque removed from cheques in hand',0,cheque['base_amount'],0,cheque['amount'])]
        source_type='ChequeBounce' if action=='bounce' else 'ChequeReturn'
    elif action=='replace':
        lines=[(accounts['1120'],None,split['station_id'],split['cost_center_id'],'Replacement cheque received into cheques in hand',cheque['base_amount'],0,cheque['amount'],0),
               (accounts['1200'],cheque['customer_id'],split['station_id'],split['cost_center_id'],'Replacement cheque applied to customer receivable',0,cheque['base_amount'],0,cheque['amount'])]
        source_type='ChequeReplacement'
    else:
        raise ValueError('Unsupported cheque accounting action.')
    base_debits=sum(line[5] for line in lines); base_credits=sum(line[6] for line in lines)
    if round(base_debits-base_credits,2)!=0: raise ValueError('Cheque journal is out of balance.')
    voucher_date=date.today().isoformat(); voucher_no=f"CHQ-{cheque['id']:06d}-{action.upper()}"
    db.execute("INSERT INTO journal_headers(organization_id,voucher_no,voucher_date,voucher_type,reference_no,currency_id,exchange_rate,source_type,source_id,status,created_by,posted_at) VALUES (?,?,?,?,?,?,?,?,?,'posted',1,CURRENT_TIMESTAMP)",
               (organization_id,voucher_no,voucher_date,'CHQ',cheque['cheque_no'],cheque['currency_id'],cheque['currency_rate'],source_type,cheque['id']))
    journal_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
    for line_no,line in enumerate(lines,1):
        account_id,customer_id,station_id,cost_center_id,description,debit,credit,currency_debit,currency_credit=line
        db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,customer_id,station_id,cost_center_id,instrument_no,instrument_date,description,debit,credit,currency_debit,currency_credit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (journal_id,line_no,account_id,customer_id,station_id,cost_center_id,cheque['cheque_no'],cheque['cheque_date'],description,debit,credit,currency_debit,currency_credit))
    db.execute("INSERT INTO approval_events(entity_type,entity_id,action,actor_id,note) VALUES ('journal',?,'posted',1,?)",
               (journal_id,f'Cheque lifecycle {action} posted'))
    db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'posted','journal',?,?)",
               (organization_id,journal_id,f'Cheque {action} journal posted for cheque {cheque["id"]}'))
    return {'journal_id':journal_id,'voucher_no':voucher_no}


class Handler(BaseHTTPRequestHandler):
    server_version = "ERPReplica/1.0"
    def send_json(self, code, payload):
        raw=json.dumps(payload,default=json_default).encode()
        origin=self.headers.get('Origin')
        origin_url=urlparse(origin)
        allow_origin=origin if origin_url.scheme=='http' and origin_url.hostname in {'localhost','127.0.0.1'} else 'http://localhost:5173'
        self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Access-Control-Allow-Origin',allow_origin); self.send_header('Vary','Origin'); self.send_header('Access-Control-Allow-Headers','Content-Type'); self.send_header('Access-Control-Allow-Methods','GET,POST,PUT,OPTIONS'); self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self): self.send_json(204,{})
    def do_GET(self):
        path=urlparse(self.path).path
        with connect() as db:
            if path == '/api/health': return self.send_json(200,{'status':'ok','database':'separate-demo-sqlite'})
            if path == '/api/receipt-options':
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                customers=[dict(r) for r in db.execute("SELECT id,code,display_name,account_id FROM customers WHERE organization_id=? AND active=1 ORDER BY display_name",(org,)).fetchall()]
                stations=[dict(r) for r in db.execute("SELECT id,code,name FROM stations WHERE organization_id=? ORDER BY name",(org,)).fetchall()]
                centers=[dict(r) for r in db.execute("SELECT id,code,name FROM cost_centers WHERE organization_id=? ORDER BY name",(org,)).fetchall()]
                accounts=[dict(r) for r in db.execute("SELECT id,code,name,account_type,account_head_type FROM accounts WHERE organization_id=? AND active=1 AND account_level=4 ORDER BY code",(org,)).fetchall()]
                currencies=[dict(r) for r in db.execute("SELECT code,name,minor_units FROM currencies ORDER BY code").fetchall()]
                employees=[dict(r) for r in db.execute("SELECT DISTINCT employee_label AS name FROM journal_lines WHERE TRIM(employee_label)<>'' ORDER BY employee_label").fetchall()]
                if not employees: employees=[{'name':'Demo User'}]
                banks=[dict(r) for r in db.execute("SELECT id,code,name FROM bank_masters WHERE organization_id=? AND active=1 ORDER BY name",(org,)).fetchall()]
                return self.send_json(200,{'companies':[{'code':'DEMO','name':'Demo Company'}],'customers':customers,'stations':stations,'cost_centers':centers,'accounts':accounts,'currencies':currencies,'employees':employees,'banks':banks})
            if path == '/api/receipt-invoices':
                query=parse_qs(urlparse(self.path).query)
                try: customer_id=int(query.get('customer_id',[''])[0])
                except ValueError: return self.send_json(400,{'error':'Select a customer to load invoices.'})
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                customer=db.execute("SELECT id,account_id FROM customers WHERE id=? AND organization_id=? AND active=1",(customer_id,org)).fetchone()
                if customer is None: return self.send_json(400,{'error':'Choose an active customer.'})
                invoices=db.execute("""SELECT j.id AS journal_id,j.voucher_no AS invoice_no,j.voucher_date AS invoice_date,
                    c.code AS currency,j.exchange_rate,SUM(l.debit-l.credit) AS invoiced_amount
                    FROM journal_headers j JOIN journal_lines l ON l.journal_id=j.id
                    JOIN currencies c ON c.id=j.currency_id
                    WHERE j.organization_id=? AND j.status='posted' AND j.source_type IN ('SalesInvoice','Invoice')
                      AND l.customer_id=? AND l.account_id=? AND l.debit>l.credit
                    GROUP BY j.id ORDER BY j.voucher_date,j.id""",(org,customer_id,customer['account_id'])).fetchall()
                result=[]
                for invoice in invoices:
                    allocated=db.execute("""SELECT COALESCE(SUM(l.gross_amount),0) FROM receipt_lines l
                        JOIN receipt_headers h ON h.id=l.receipt_id
                        WHERE h.organization_id=? AND h.status='posted' AND l.customer_id=? AND l.invoice_no=?""",(org,customer_id,invoice['invoice_no'])).fetchone()[0]
                    outstanding=round(float(invoice['invoiced_amount'])-float(allocated),2)
                    if outstanding>0: result.append({**dict(invoice),'outstanding_amount':outstanding})
                return self.send_json(200,result)
            if path == '/api/payments':
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                rows=db.execute("""SELECT h.id,h.payment_no,h.payment_date,h.status,c.code AS currency,
                    ROUND(SUM(l.gross_amount),2) AS gross_amount,ROUND(SUM(l.discount_amount),2) AS discount_amount,
                    ROUND(SUM(l.gross_amount-l.discount_amount),2) AS net_amount,
                    GROUP_CONCAT(DISTINCT a.name) AS payee,MAX(j.voucher_no) AS voucher_no,h.remarks
                    FROM payment_headers h JOIN currencies c ON c.id=h.currency_id
                    JOIN payment_lines l ON l.payment_id=h.id JOIN accounts a ON a.id=l.account_id
                    LEFT JOIN journal_headers j ON j.source_type='Payment' AND j.source_id=h.id
                    WHERE h.organization_id=? GROUP BY h.id ORDER BY h.payment_date DESC,h.id DESC""",(org,)).fetchall()
                return self.send_json(200,[dict(r) for r in rows])
            if path == '/api/expenses':
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                rows=db.execute("""SELECT h.id,h.expense_no,h.expense_date,h.reference_no,h.payment_type,h.status,
                    c.code AS currency,a.name AS from_account,ROUND(SUM(l.currency_amount),2) AS total,
                    MAX(j.voucher_no) AS voucher_no,u.display_name AS creator,h.remarks
                    FROM expense_headers h JOIN currencies c ON c.id=h.currency_id
                    JOIN accounts a ON a.id=h.from_account_id JOIN expense_lines l ON l.expense_id=h.id
                    LEFT JOIN journal_headers j ON j.source_type='Expense' AND j.source_id=h.id
                    LEFT JOIN users u ON u.id=h.created_by WHERE h.organization_id=?
                    GROUP BY h.id ORDER BY h.expense_date DESC,h.id DESC""",(org,)).fetchall()
                return self.send_json(200,[dict(r) for r in rows])
            if path == '/api/receipts':
                rows=db.execute("""SELECT h.id,h.receipt_no,h.receipt_date,h.status,
                    CASE WHEN h.status='posted' THEN 'approved' ELSE h.status END AS approval_status,
                    MAX(CASE WHEN j.status='posted' THEN 1 ELSE 0 END) AS journal_posted,c.code AS currency,
                    SUM(l.gross_amount) gross_amount,SUM(l.discount_amount) discount_amount,
                    SUM(l.gross_amount-l.discount_amount) net_amount,
                    GROUP_CONCAT(DISTINCT cu.display_name) customer,
                    GROUP_CONCAT(DISTINCT st.name) station,MAX(j.voucher_no) voucher_no,
                    h.remarks description,MAX(u.display_name) creator
                    FROM receipt_headers h JOIN currencies c ON c.id=h.currency_id
                    JOIN receipt_lines l ON l.receipt_id=h.id JOIN customers cu ON cu.id=l.customer_id
                    LEFT JOIN stations st ON st.id=l.station_id
                    LEFT JOIN journal_headers j ON j.source_type='CollectionReceipt' AND j.source_id=h.id
                    LEFT JOIN users u ON u.id=h.created_by
                    GROUP BY h.id ORDER BY h.receipt_date DESC,h.id DESC""").fetchall()
                return self.send_json(200,[dict(r) for r in rows])
            if path.startswith('/api/receipts/') and path.endswith('/history'):
                try: receipt_id=int(path.split('/')[-2])
                except ValueError: return self.send_json(404,{'error':'Receipt not found'})
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                if db.execute("SELECT 1 FROM receipt_headers WHERE id=? AND organization_id=?",(receipt_id,org)).fetchone() is None: return self.send_json(404,{'error':'Receipt not found'})
                events=[]
                for r in db.execute("SELECT action AS event,note AS detail,created_at FROM approval_events WHERE entity_type='receipt' AND entity_id=? ORDER BY created_at,id",(receipt_id,)).fetchall(): events.append(dict(r))
                for r in db.execute("SELECT action AS event,detail,created_at FROM audit_log WHERE organization_id=? AND entity_type='receipt' AND entity_id=? ORDER BY created_at,id",(org,receipt_id)).fetchall(): events.append(dict(r))
                events.sort(key=lambda x:x['created_at'])
                return self.send_json(200,events)
            if path.startswith('/api/receipts/'):
                try: receipt_id=int(path.rsplit('/',1)[1])
                except ValueError: return self.send_json(404,{'error':'Receipt not found'})
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                header=db.execute("SELECT h.*,c.code AS currency FROM receipt_headers h JOIN currencies c ON c.id=h.currency_id WHERE h.id=? AND h.organization_id=?",(receipt_id,org)).fetchone()
                if header is None: return self.send_json(404,{'error':'Receipt not found'})
                lines=db.execute("SELECT customer_id,station_id,cost_center_id,employee_label,comment,invoice_no,invoice_rate,payment_type,gross_amount,discount_amount,instrument_no,instrument_date FROM receipt_lines WHERE receipt_id=? ORDER BY line_no",(receipt_id,)).fetchall()
                deposits=db.execute("SELECT d.account_id,d.customer_id,d.method,d.reference,d.bank_label,d.instrument_no,d.instrument_date,d.currency_date,d.station_id,d.cost_center_id,d.amount FROM deposit_splits d WHERE d.receipt_id=? ORDER BY d.line_no",(receipt_id,)).fetchall()
                cheques=db.execute("SELECT cheque_no,cheque_date,currency_date,signatory,payer_bank,customer_id,currency_rate,amount,locker FROM cheques WHERE receipt_id=? AND replacement_of_id IS NULL ORDER BY id",(receipt_id,)).fetchall()
                return self.send_json(200,{**dict(header),'lines':[dict(r) for r in lines],'deposits':[dict(r) for r in deposits],'cheques':[dict(r) for r in cheques]})
            if path == '/api/accounts':
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                rows=db.execute("SELECT id,code,name,parent_id,account_type,account_level,account_head_type,account_note,is_control,active FROM accounts WHERE organization_id=? AND active=1 ORDER BY code",(org,)).fetchall()
                return self.send_json(200,[dict(r) for r in rows])
            if path == '/api/report-options':
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                return self.send_json(200,{
                    'companies':[{'code':'DEMO','name':'Demo Company'}],
                    'stations':[dict(r) for r in db.execute("SELECT id,code,name FROM stations WHERE organization_id=? ORDER BY name",(org,)).fetchall()],
                    'cost_center_heads':[dict(r) for r in db.execute("SELECT id,code,name FROM cost_center_heads WHERE organization_id=? ORDER BY name",(org,)).fetchall()],
                    'cost_centers':[dict(r) for r in db.execute("SELECT id,code,name,head_id FROM cost_centers WHERE organization_id=? ORDER BY name",(org,)).fetchall()],
                    'employees':[{'name':r['employee_label']} for r in db.execute("SELECT DISTINCT employee_label FROM journal_lines WHERE TRIM(employee_label)<>'' ORDER BY employee_label").fetchall()],
                    'currencies':[dict(r) for r in db.execute("SELECT code,name FROM currencies ORDER BY code").fetchall()],
                    'account_head_types':[{'name':r['account_head_type']} for r in db.execute("SELECT DISTINCT account_head_type FROM accounts WHERE organization_id=? AND TRIM(account_head_type)<>'' ORDER BY account_head_type",(org,)).fetchall()]
                })
            if path == '/api/cheques':
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                rows=db.execute("""SELECT q.id,h.receipt_no AS doc_no,h.receipt_date AS doc_date,cu.display_name AS customer,
                    cur.code AS currency,q.currency_rate,q.amount,q.cheque_no,q.cheque_date,q.currency_date,q.signatory,q.payer_bank,
                    h.call_label,h.cheque_type,q.status,q.locker,q.remarks,q.replacement_of_id
                    FROM cheques q JOIN receipt_headers h ON h.id=q.receipt_id
                    JOIN customers cu ON cu.id=q.customer_id JOIN currencies cur ON cur.id=q.currency_id
                    WHERE q.organization_id=? ORDER BY q.cheque_date DESC,q.id DESC""",(org,)).fetchall()
                return self.send_json(200,[dict(r) for r in rows])
            if path == '/api/cheques/return-batch':
                ids=body.get('cheque_ids',[])
                if not isinstance(ids,list) or not ids: raise ValueError('Select at least one cheque to return.')
                try: ids=list(dict.fromkeys(int(value) for value in ids))
                except (TypeError,ValueError): raise ValueError('The selected cheque list is invalid.')
                return_date=date.fromisoformat(body.get('return_date',date.today().isoformat())).isoformat()
                medium=str(body.get('transfer_medium','Customer')).strip() or 'Customer'
                reason=f"Returned on {return_date} via {medium}"+(f": {str(body.get('remarks','')).strip()}" if str(body.get('remarks','')).strip() else '')
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                    db.execute('BEGIN IMMEDIATE')
                    total=0.0;vouchers=[]
                    for cheque_id in ids:
                        cheque=db.execute("SELECT * FROM cheques WHERE id=? AND organization_id=?",(cheque_id,org)).fetchone()
                        if cheque is None: raise ValueError('One of the selected cheques was not found.')
                        next_status={'in_hand':'returned','deposited':'returned','transferred':'returned','bounced':'returned'}.get(cheque['status'])
                        if next_status is None: raise ValueError('One of the selected cheques is no longer eligible for return. Refresh the list and try again.')
                        total+=float(cheque['amount'])
                        receipt_posted=db.execute("SELECT 1 FROM journal_headers WHERE organization_id=? AND source_type='CollectionReceipt' AND source_id=? AND status='posted'",(org,cheque['receipt_id'])).fetchone()
                        # Returning a bounced cheque is a custody event only. The bounce
                        # already restored the receivable, so a second journal would
                        # incorrectly reopen it again.
                        physical_return=cheque['status']=='bounced'
                        voucher=post_cheque_lifecycle_journal(db,org,cheque,'return')['voucher_no'] if receipt_posted and not physical_return else None
                        if voucher: vouchers.append(voucher)
                        db.execute("UPDATE cheques SET status=?,remarks=CASE WHEN ?='' THEN remarks ELSE ? END WHERE id=?",(next_status,reason,reason,cheque_id))
                        event_type='physical_return' if physical_return else 'returned'
                        db.execute("INSERT INTO cheque_events(cheque_id,event_type,from_status,to_status,reason,actor_id) VALUES (?,?,?,?,?,1)",(cheque_id,event_type,cheque['status'],next_status,reason))
                        db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'return','cheque',?,?)",(org,cheque_id,reason))
                    db.commit()
                return self.send_json(200,{'returned_count':len(ids),'amount':round(total,2),'vouchers':vouchers})
            if path.startswith('/api/cheques/') and path.endswith('/events'):
                cheque_id=int(path.strip('/').split('/')[2])
                events=db.execute("SELECT id,event_type,from_status,to_status,reason,reference,occurred_at FROM cheque_events WHERE cheque_id=? ORDER BY occurred_at,id",(cheque_id,)).fetchall()
                return self.send_json(200,[dict(r) for r in events])
            if path == '/api/customers':
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                return self.send_json(200,[dict(r) for r in db.execute("SELECT id,code,display_name FROM customers WHERE organization_id=? AND active=1 ORDER BY display_name",(org,)).fetchall()])
            if path == '/api/reports/collection-history':
                query=parse_qs(urlparse(self.path).query)
                from_date=date.fromisoformat(query.get('from',['2026-10-01'])[0]).isoformat()
                to_date=date.fromisoformat(query.get('to',['2026-10-31'])[0]).isoformat()
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                customer=query.get('customer_id',[None])[0] or None
                station=query.get('station_id',[None])[0] or None
                cost_center=query.get('cost_center_id',[None])[0] or None
                if customer is not None: customer=int(customer)
                if station is not None: station=int(station)
                if cost_center is not None: cost_center=int(cost_center)
                all_detail_accounts=query.get('all_detail_accounts',['0'])[0]=='1'
                if not all_detail_accounts and customer is None:
                    return self.send_json(400,{'error':'Select a customer or include all detail accounts.'})
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                rows=db.execute("""SELECT h.id,h.receipt_no AS doc_no,h.receipt_date AS voucher_date,
                    COALESCE((SELECT GROUP_CONCAT(DISTINCT cu.display_name) FROM receipt_lines rl JOIN customers cu ON cu.id=rl.customer_id WHERE rl.receipt_id=h.id AND (? IS NULL OR rl.customer_id=?)),'') AS customer,
                    COALESCE((SELECT GROUP_CONCAT(DISTINCT rl.payment_type) FROM receipt_lines rl WHERE rl.receipt_id=h.id AND (? IS NULL OR rl.customer_id=?)),'') AS type,
                    COALESCE((SELECT GROUP_CONCAT(DISTINCT NULLIF(rl.invoice_no,'')) FROM receipt_lines rl WHERE rl.receipt_id=h.id AND (? IS NULL OR rl.customer_id=?)),'') AS source_no,
                    COALESCE((SELECT GROUP_CONCAT(DISTINCT a.name) FROM deposit_splits d LEFT JOIN accounts a ON a.id=d.account_id WHERE d.receipt_id=h.id),'') AS deposit_in,
                    COALESCE((SELECT GROUP_CONCAT(DISTINCT NULLIF(d.reference,'')) FROM deposit_splits d WHERE d.receipt_id=h.id),h.remarks,'') AS comments,
                    COALESCE((SELECT GROUP_CONCAT(DISTINCT NULLIF(d.bank_label,'')) FROM deposit_splits d WHERE d.receipt_id=h.id),'') AS cheque_bank,
                    COALESCE((SELECT GROUP_CONCAT(DISTINCT NULLIF(d.instrument_no,'')) FROM deposit_splits d WHERE d.receipt_id=h.id),'') AS instrument_no,
                    COALESCE((SELECT GROUP_CONCAT(DISTINCT NULLIF(d.instrument_date,'')) FROM deposit_splits d WHERE d.receipt_id=h.id),'') AS instrument_date,
                    COALESCE((SELECT SUM((rl.gross_amount-rl.discount_amount)*COALESCE(h.exchange_rate,1)) FROM receipt_lines rl WHERE rl.receipt_id=h.id AND (? IS NULL OR rl.customer_id=?)),0) AS amount,
                    cur.code AS currency,h.status
                    FROM receipt_headers h JOIN currencies cur ON cur.id=h.currency_id
                    WHERE h.organization_id=? AND h.receipt_date>=? AND h.receipt_date<=?
                    AND (? IS NULL OR EXISTS (SELECT 1 FROM receipt_lines rl WHERE rl.receipt_id=h.id AND rl.customer_id=?))
                    AND (? IS NULL OR EXISTS (SELECT 1 FROM receipt_lines rl WHERE rl.receipt_id=h.id AND rl.station_id=?) OR EXISTS (SELECT 1 FROM deposit_splits d WHERE d.receipt_id=h.id AND d.station_id=?))
                    AND (? IS NULL OR EXISTS (SELECT 1 FROM receipt_lines rl WHERE rl.receipt_id=h.id AND rl.cost_center_id=?) OR EXISTS (SELECT 1 FROM deposit_splits d WHERE d.receipt_id=h.id AND d.cost_center_id=?))
                    ORDER BY h.receipt_date,h.id""",
                    (customer,customer,customer,customer,customer,customer,customer,customer,org,from_date,to_date,
                     customer,customer,station,station,station,cost_center,cost_center,cost_center)).fetchall()
                result=[dict(row) for row in rows]
                total=round(sum(float(row['amount'] or 0) for row in result),2)
                return self.send_json(200,{'from_date':from_date,'to_date':to_date,'currency':'PKR','all_detail_accounts':all_detail_accounts,
                    'totals':{'amount':total},'rows':result,'demo_only':True})
            if path == '/api/journals':
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                rows=db.execute("SELECT h.id,h.voucher_no,h.voucher_date,h.voucher_type,h.reference_no,h.status,COUNT(l.id) AS line_count,SUM(l.debit) AS debit_total FROM journal_headers h JOIN journal_lines l ON l.journal_id=h.id WHERE h.organization_id=? AND h.source_type='Voucher' GROUP BY h.id ORDER BY h.voucher_date DESC,h.id DESC",(org,)).fetchall()
                return self.send_json(200,[dict(r) for r in rows])
            if path == '/api/reports/trial-balance':
                query=parse_qs(urlparse(self.path).query)
                from_date=date.fromisoformat(query.get('from',[date.today().replace(day=1).isoformat()])[0]).isoformat()
                to_date=date.fromisoformat(query.get('to',[date.today().isoformat()])[0]).isoformat()
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                station=query.get('station_id',[None])[0] or None; cost_center=query.get('cost_center_id',[None])[0] or None
                if station is not None: station=int(station)
                if cost_center is not None: cost_center=int(cost_center)
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                rows=db.execute("""SELECT a.id,a.code AS account_code,a.name AS account_title,
                    COALESCE(h.code,'') AS account_head_code,h.name AS account_head_title,
                    ROUND(COALESCE(SUM(CASE WHEN j.voucher_date<? THEN l.debit-l.credit ELSE 0 END),0),2) AS opening_balance,
                    ROUND(COALESCE(SUM(CASE WHEN j.voucher_date>=? AND j.voucher_date<=? THEN l.debit ELSE 0 END),0),2) AS debit,
                    ROUND(COALESCE(SUM(CASE WHEN j.voucher_date>=? AND j.voucher_date<=? THEN l.credit ELSE 0 END),0),2) AS credit,
                    ROUND(COALESCE(SUM(CASE WHEN j.voucher_date<=? THEN l.debit-l.credit ELSE 0 END),0),2) AS balance
                    FROM accounts a LEFT JOIN accounts h ON h.id=a.parent_id
                    LEFT JOIN journal_lines l ON l.account_id=a.id AND (? IS NULL OR l.station_id=?) AND (? IS NULL OR l.cost_center_id=?)
                    LEFT JOIN journal_headers j ON j.id=l.journal_id AND j.organization_id=? AND j.status='posted'
                    WHERE a.organization_id=? AND a.account_level=4 AND a.active=1
                    GROUP BY a.id ORDER BY a.code,a.name""",
                    (from_date,from_date,to_date,from_date,to_date,to_date,station,station,cost_center,cost_center,org,org)).fetchall()
                result=[dict(r) for r in rows]
                totals={key:round(sum(float(r[key] or 0) for r in result),2) for key in ('opening_balance','debit','credit','balance')}
                return self.send_json(200,{'from_date':from_date,'to_date':to_date,'totals':totals,'rows':result})
            if path == '/api/reports/balance-sheet':
                query=parse_qs(urlparse(self.path).query)
                as_of=date.fromisoformat(query.get('as_of',[date.today().isoformat()])[0]).isoformat()
                station=query.get('station_id',[None])[0] or None; cost_center=query.get('cost_center_id',[None])[0] or None
                if station is not None: station=int(station)
                if cost_center is not None: cost_center=int(cost_center)
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                raw=db.execute("""SELECT a.id,a.code,a.name,a.account_type,COALESCE(SUM(CASE WHEN j.id IS NOT NULL THEN l.debit ELSE 0 END),0) debit,COALESCE(SUM(CASE WHEN j.id IS NOT NULL THEN l.credit ELSE 0 END),0) credit
                    FROM accounts a LEFT JOIN journal_lines l ON l.account_id=a.id AND (? IS NULL OR l.station_id=?) AND (? IS NULL OR l.cost_center_id=?)
                    LEFT JOIN journal_headers j ON j.id=l.journal_id AND j.organization_id=? AND j.status='posted' AND j.voucher_date<=?
                    WHERE a.organization_id=? AND a.account_level=4 AND a.active=1 GROUP BY a.id ORDER BY a.code,a.name""",
                    (station,station,cost_center,cost_center,org,as_of,org)).fetchall()
                sections=[{'key':key,'label':label,'accounts':[]} for key,label in [('non_current_assets','Non Current Assets'),('current_assets','Current Assets'),('longterm_liabilities','Longterm Liabilities'),('current_liabilities','Current Liablities'),('equity','ShareHolder\'s Equity'),('profit_loss','Profit And Loss')]]
                section_map={item['key']:item for item in sections}
                for row in raw:
                    typ=row['account_type']
                    if typ=='asset': key='current_assets'; balance=float(row['debit'] or 0)-float(row['credit'] or 0)
                    elif typ=='liability': key='current_liabilities'; balance=float(row['credit'] or 0)-float(row['debit'] or 0)
                    elif typ=='equity': key='equity'; balance=float(row['credit'] or 0)-float(row['debit'] or 0)
                    elif typ in ('income','expense'):
                        key='profit_loss'; balance=(float(row['credit'] or 0)-float(row['debit'] or 0)) if typ=='income' else (float(row['debit'] or 0)-float(row['credit'] or 0))
                    else: continue
                    if round(balance,2): section_map[key]['accounts'].append({'code':row['code'],'name':row['name'],'balance':round(balance,2)})
                for section in sections: section['total']=round(sum(a['balance'] for a in section['accounts']),2)
                totals={'assets':round(section_map['non_current_assets']['total']+section_map['current_assets']['total'],2),'liabilities':round(section_map['longterm_liabilities']['total']+section_map['current_liabilities']['total'],2),'equity':section_map['equity']['total'],'profit_loss':section_map['profit_loss']['total']}
                totals['balance_check']=round(totals['assets']-totals['liabilities']-totals['equity']-totals['profit_loss'],2)
                return self.send_json(200,{'as_of':as_of,'currency':'PKR','sections':sections,'totals':totals,'demo_only':True})
            if path == '/api/reports/profit-loss':
                query=parse_qs(urlparse(self.path).query)
                today=date.today(); month_start=today.replace(day=1)
                from_date=date.fromisoformat(query.get('from',[month_start.isoformat()])[0]).isoformat()
                to_date=date.fromisoformat(query.get('to',[today.isoformat()])[0]).isoformat()
                if from_date>to_date: return self.send_json(400,{'error':'Start date must be on or before To date.'})
                station=query.get('station_id',[None])[0] or None; cost_center=query.get('cost_center_id',[None])[0] or None
                if station is not None: station=int(station)
                if cost_center is not None: cost_center=int(cost_center)
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                raw=db.execute("""SELECT a.name,a.account_type,COALESCE(SUM(l.debit),0) debit,COALESCE(SUM(l.credit),0) credit
                    FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id
                    JOIN accounts a ON a.id=l.account_id
                    WHERE j.organization_id=? AND j.status='posted' AND j.voucher_date>=? AND j.voucher_date<=?
                    AND a.account_level=4 AND a.active=1 AND a.account_type IN ('income','expense')
                    AND (? IS NULL OR l.station_id=?) AND (? IS NULL OR l.cost_center_id=?)
                    GROUP BY a.id ORDER BY a.code,a.name""",(org,from_date,to_date,station,station,cost_center,cost_center)).fetchall()
                buckets={'sales':0.0,'cost_of_goods_sold':0.0,'other_income':0.0,'administrative_expense':0.0,'selling_marketing_expense':0.0,'other_expenses':0.0,'financial_cost':0.0,'income_tax':0.0}
                for row in raw:
                    name=row['name'].lower(); amount=float(row['credit'] or 0)-float(row['debit'] or 0) if row['account_type']=='income' else float(row['debit'] or 0)-float(row['credit'] or 0)
                    if row['account_type']=='income': bucket='other_income' if 'other income' in name else 'sales'
                    elif 'cost of goods' in name or 'cost of sales' in name: bucket='cost_of_goods_sold'
                    elif 'administrative' in name: bucket='administrative_expense'
                    elif 'selling' in name or 'marketing' in name: bucket='selling_marketing_expense'
                    elif 'financial cost' in name or 'finance cost' in name: bucket='financial_cost'
                    elif 'income tax' in name or 'tax expense' in name: bucket='income_tax'
                    else: bucket='other_expenses'
                    buckets[bucket]+=amount
                gross=buckets['sales']-buckets['cost_of_goods_sold']
                total_profit=gross+buckets['other_income']
                total_expenses=buckets['administrative_expense']+buckets['selling_marketing_expense']+buckets['other_expenses']
                operating=total_profit-total_expenses; before_tax=operating-buckets['financial_cost']; net=before_tax-buckets['income_tax']
                values=[('Sales',buckets['sales'],'detail'),('Cost of Goods Sold',buckets['cost_of_goods_sold'],'detail'),('Gross Profit',gross,'total'),('Other Income',buckets['other_income'],'detail'),('Total Profit',total_profit,'total'),('Administrative Expense',buckets['administrative_expense'],'detail'),('Selling And Marketing Expenses',buckets['selling_marketing_expense'],'detail'),('Other Expenses',buckets['other_expenses'],'detail'),('Total Expenses',total_expenses,'total'),('Operating Profit',operating,'total'),('Financial Cost',buckets['financial_cost'],'detail'),('Profit Before Taxation',before_tax,'total'),('Income Tax',buckets['income_tax'],'detail'),('Net Profit',net,'grand_total')]
                rows=[{'label':label,'amount':round(amount,2),'kind':kind} for label,amount,kind in values]
                return self.send_json(200,{'from_date':from_date,'to_date':to_date,'currency':'PKR','rows':rows,'totals':{'sales':round(buckets['sales'],2),'total_expenses':round(total_expenses,2),'net_profit':round(net,2)},'demo_only':True})
            if path == '/api/reports/profit-loss-comparison':
                query=parse_qs(urlparse(self.path).query)
                today=date.today(); this_month=today.replace(day=1)
                ranges=[]
                for slot,default_from,default_to in [('1',date(2026,9,1),date(2026,9,30)),('2',this_month,today)]:
                    start=date.fromisoformat(query.get(f'from{slot}',[default_from.isoformat()])[0]).isoformat()
                    end=date.fromisoformat(query.get(f'to{slot}',[default_to.isoformat()])[0]).isoformat()
                    if start>end: return self.send_json(400,{'error':f'Range {slot} start date must be on or before its end date.'})
                    ranges.append((start,end))
                station=query.get('station_id',[None])[0] or None; cost_center=query.get('cost_center_id',[None])[0] or None
                if station is not None: station=int(station)
                if cost_center is not None: cost_center=int(cost_center)
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                def statement_for(start,end):
                    raw=db.execute("""SELECT a.name,a.account_type,COALESCE(SUM(l.debit),0) debit,COALESCE(SUM(l.credit),0) credit
                        FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id
                        WHERE j.organization_id=? AND j.status='posted' AND j.voucher_date>=? AND j.voucher_date<=?
                        AND a.account_level=4 AND a.active=1 AND a.account_type IN ('income','expense')
                        AND (? IS NULL OR l.station_id=?) AND (? IS NULL OR l.cost_center_id=?)
                        GROUP BY a.id ORDER BY a.code,a.name""",(org,start,end,station,station,cost_center,cost_center)).fetchall()
                    buckets={'sales':0.0,'cost_of_goods_sold':0.0,'other_income':0.0,'administrative_expense':0.0,'selling_marketing_expense':0.0,'other_expenses':0.0,'financial_cost':0.0,'income_tax':0.0}
                    for account in raw:
                        name=account['name'].lower(); amount=float(account['credit'] or 0)-float(account['debit'] or 0) if account['account_type']=='income' else float(account['debit'] or 0)-float(account['credit'] or 0)
                        if account['account_type']=='income': bucket='other_income' if 'other income' in name else 'sales'
                        elif 'cost of goods' in name or 'cost of sales' in name: bucket='cost_of_goods_sold'
                        elif 'administrative' in name: bucket='administrative_expense'
                        elif 'selling' in name or 'marketing' in name: bucket='selling_marketing_expense'
                        elif 'financial cost' in name or 'finance cost' in name: bucket='financial_cost'
                        elif 'income tax' in name or 'tax expense' in name: bucket='income_tax'
                        else: bucket='other_expenses'
                        buckets[bucket]+=amount
                    gross=buckets['sales']-buckets['cost_of_goods_sold']; total_profit=gross+buckets['other_income']
                    expenses=buckets['administrative_expense']+buckets['selling_marketing_expense']+buckets['other_expenses']
                    operating=total_profit-expenses; before_tax=operating-buckets['financial_cost']; net=before_tax-buckets['income_tax']
                    return [buckets['sales'],buckets['cost_of_goods_sold'],gross,buckets['other_income'],total_profit,buckets['administrative_expense'],buckets['selling_marketing_expense'],buckets['other_expenses'],expenses,operating,buckets['financial_cost'],before_tax,buckets['income_tax'],net]
                labels=['Sales','Cost of Goods Sold','Gross Profit','Other Income','Total Profit','Administrative Expense','Selling And Marketing Expenses','Other Expenses','Total Expenses','Operating Profit','Financial Cost','Profit Before Taxation','Income Tax','Net Profit']
                kinds=['detail','detail','total','detail','total','detail','detail','detail','total','total','detail','total','detail','grand_total']
                first=statement_for(*ranges[0]); second=statement_for(*ranges[1])
                rows=[{'label':label,'range_1':round(a,2),'range_2':round(b,2),'difference':round(b-a,2),'kind':kind} for label,a,b,kind in zip(labels,first,second,kinds)]
                return self.send_json(200,{'range_1':{'from':ranges[0][0],'to':ranges[0][1]},'range_2':{'from':ranges[1][0],'to':ranges[1][1]},'currency':'PKR','rows':rows,'demo_only':True})
            if path == '/api/reports/wise-profit-and-loss':
                query=parse_qs(urlparse(self.path).query)
                from_date=date.fromisoformat(query.get('from',['2026-10-01'])[0]).isoformat()
                to_date=date.fromisoformat(query.get('to',['2026-10-31'])[0]).isoformat()
                mode=query.get('mode',['station'])[0]
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                if mode not in ('station','cost_center'): return self.send_json(400,{'error':'Mode must be station or cost_center.'})
                station=query.get('station_id',[None])[0] or None; cost_center=query.get('cost_center_id',[None])[0] or None
                if station is not None: station=int(station)
                if cost_center is not None: cost_center=int(cost_center)
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                dimension='station_id' if mode=='station' else 'cost_center_id'
                master='stations' if mode=='station' else 'cost_centers'
                raw=db.execute(f"""SELECT a.id,a.code,a.name,a.account_type,a.account_note,
                    l.{dimension} AS dimension_id,SUM(l.debit) AS debit,SUM(l.credit) AS credit
                    FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id
                    WHERE j.organization_id=? AND j.status='posted' AND j.voucher_date>=? AND j.voucher_date<=?
                    AND a.account_level=4 AND a.active=1 AND a.account_type IN ('income','expense')
                    AND (? IS NULL OR l.station_id=?) AND (? IS NULL OR l.cost_center_id=?)
                    GROUP BY a.id,l.{dimension} ORDER BY a.code,a.name,l.{dimension}""",
                    (org,from_date,to_date,station,station,cost_center,cost_center)).fetchall()
                columns=[{'key':str(r['id']),'label':r['name'],'id':r['id']} for r in db.execute(
                    f"SELECT id,name FROM {master} WHERE organization_id=? ORDER BY name",(org,)).fetchall()]
                if any(item['dimension_id'] is None for item in raw):
                    columns.append({'key':'unassigned','label':'Unassigned','id':None})
                if not columns: columns=[{'key':'unassigned','label':'Unassigned','id':None}]
                bucket_names=['sales','cost_of_goods_sold','other_income','administrative_expense','selling_marketing_expense','other_expenses','financial_cost','income_tax']
                bucket_labels={'sales':'Sales','cost_of_goods_sold':'Cost of Goods Sold','other_income':'Other Income',
                    'administrative_expense':'Administrative Expense','selling_marketing_expense':'Selling And Marketing Expenses',
                    'other_expenses':'Other Expenses','financial_cost':'Financial Cost','income_tax':'Income Tax'}
                account_rows={}
                amounts={key:{column['key']:0.0 for column in columns} for key in bucket_names}
                for item in raw:
                    column_key=str(item['dimension_id']) if item['dimension_id'] is not None else 'unassigned'
                    if column_key not in amounts['sales']: continue
                    name=item['name'].lower()
                    if item['account_type']=='income': bucket='other_income' if 'other income' in name else 'sales'
                    elif 'cost of goods' in name or 'cost of sales' in name: bucket='cost_of_goods_sold'
                    elif 'administrative' in name: bucket='administrative_expense'
                    elif 'selling' in name or 'marketing' in name: bucket='selling_marketing_expense'
                    elif 'financial cost' in name or 'finance cost' in name: bucket='financial_cost'
                    elif 'income tax' in name or 'tax expense' in name: bucket='income_tax'
                    else: bucket='other_expenses'
                    amount=float(item['credit'] or 0)-float(item['debit'] or 0) if item['account_type']=='income' else float(item['debit'] or 0)-float(item['credit'] or 0)
                    amounts[bucket][column_key]+=amount
                    account=account_rows.setdefault(item['id'],{'key':f"account-{item['id']}",'bucket':bucket,
                        'note':item['account_note'] or bucket_labels[bucket],'account':item['name'],'code':item['code'] or '',
                        'kind':'account','values':{column['key']:0.0 for column in columns}})
                    account['values'][column_key]+=amount
                def calc(column_key):
                    b={name:amounts[name][column_key] for name in bucket_names}
                    gross=b['sales']-b['cost_of_goods_sold']; profit=gross+b['other_income']
                    expenses=b['administrative_expense']+b['selling_marketing_expense']+b['other_expenses']
                    operating=profit-expenses; before_tax=operating-b['financial_cost']; net=before_tax-b['income_tax']
                    return [b['sales'],b['cost_of_goods_sold'],gross,b['other_income'],profit,b['administrative_expense'],b['selling_marketing_expense'],b['other_expenses'],expenses,operating,b['financial_cost'],before_tax,b['income_tax'],net]
                labels=['Sales','Cost of Goods Sold','Gross Profit','Other Income','Total Profit','Administrative Expense','Selling And Marketing Expenses','Other Expenses','Total Expenses','Operating Profit','Financial Cost','Profit Before Taxation','Income Tax','Net Profit']
                keys=['sales','cost_of_goods_sold','gross_profit','other_income','total_profit','administrative_expense','selling_marketing_expense','other_expenses','total_expenses','operating_profit','financial_cost','profit_before_taxation','income_tax','net_profit']
                kinds=['section','section','total','section','total','section','section','section','total','total','section','total','section','grand_total']
                values_by_column={column['key']:calc(column['key']) for column in columns}
                rows=[]
                detail_buckets={'sales','cost_of_goods_sold','other_income','administrative_expense','selling_marketing_expense','other_expenses','financial_cost','income_tax'}
                for index,(key,label,kind) in enumerate(zip(keys,labels,kinds)):
                    rows.append({'key':key,'note':label,'account':label,'code':'','bucket':key,'kind':kind,
                        'values':{column['key']:round(values_by_column[column['key']][index],2) for column in columns}})
                    if key in detail_buckets:
                        for account in account_rows.values():
                            if account['bucket']==key:
                                rows.append({**account,'values':{col:round(value,2) for col,value in account['values'].items()}})
                return self.send_json(200,{'from_date':from_date,'to_date':to_date,'mode':mode,
                    'column_dimension':'Station' if mode=='station' else 'Cost Center','columns':columns,
                    'rows':rows,'currency':'PKR','demo_only':True})
            if path == '/api/reports/consolidated-ledger':
                query=parse_qs(urlparse(self.path).query)
                selected_id=query.get('account_id',[None])[0]
                if not selected_id: return self.send_json(400,{'error':'Select an account to view its consolidated ledger.'})
                try:
                    selected_id=int(selected_id)
                    level=int(query.get('account_level',['4'])[0])
                    from_date=date.fromisoformat(query.get('from',[date.today().replace(day=1).isoformat()])[0]).isoformat()
                    to_date=date.fromisoformat(query.get('to',[date.today().isoformat()])[0]).isoformat()
                    station=query.get('station_id',[None])[0] or None
                    cost_head=query.get('cost_center_head_id',[None])[0] or None
                    cost_center=query.get('cost_center_id',[None])[0] or None
                    for value in (station,cost_head,cost_center):
                        if value is not None: int(value)
                except (ValueError,TypeError):
                    return self.send_json(400,{'error':'Check the account, date and filter values.'})
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                if level not in (1,2,3,4): return self.send_json(400,{'error':'Select a valid account level.'})
                currency=query.get('currency',[None])[0] or None
                employee=query.get('employee',[None])[0] or None
                head_types=[value for value in query.get('account_head_types',[''])[0].split(',') if value]
                show_zero=query.get('show_zero',['0'])[0]=='1'
                view=query.get('view',['detail'])[0]
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                selected=db.execute("SELECT id,code,name,account_level FROM accounts WHERE id=? AND organization_id=? AND active=1",(selected_id,org)).fetchone()
                if selected is None: return self.send_json(404,{'error':'The selected account was not found in the demo chart.'})
                account_ids=[r['id'] for r in db.execute("""WITH RECURSIVE account_tree(id) AS (
                    SELECT id FROM accounts WHERE id=? AND organization_id=? AND active=1
                    UNION ALL SELECT a.id FROM accounts a JOIN account_tree p ON a.parent_id=p.id WHERE a.organization_id=? AND a.active=1
                ) SELECT id FROM account_tree""",(selected_id,org,org)).fetchall()]
                if not account_ids: return self.send_json(200,{'rows':[],'totals':{'opening_balance':0,'debit':0,'credit':0,'closing_balance':0},'currency':currency or 'PKR','account':dict(selected)})
                marks=','.join('?' for _ in account_ids)
                where=[f"l.account_id IN ({marks})",'a.account_level=?','j.organization_id=?',"j.status='posted'",'j.voucher_date<=?']
                params=list(account_ids)+[level,org,to_date]
                if station: where.append('l.station_id=?');params.append(int(station))
                if cost_head: where.append('cc.head_id=?');params.append(int(cost_head))
                if cost_center: where.append('l.cost_center_id=?');params.append(int(cost_center))
                if currency: where.append("COALESCE(c.code,rc.code,'PKR')=?");params.append(currency)
                if employee: where.append('l.employee_label=?');params.append(employee)
                if head_types:
                    where.append('a.account_head_type IN ('+','.join('?' for _ in head_types)+')');params.extend(head_types)
                raw=db.execute("""SELECT j.id AS journal_id,j.voucher_date,j.voucher_no,j.voucher_type,
                    a.id AS account_id,a.code AS account_code,a.name AS account_title,a.account_head_type,
                    COALESCE(parent.code,'') AS account_head_code,COALESCE(parent.name,'') AS account_head_title,
                    COALESCE(l.description,'') AS description,COALESCE(s.name,'') AS station,
                    COALESCE(cc.name,'') AS cost_center,COALESCE(c.code,rc.code,'PKR') AS transaction_currency,
                    COALESCE(l.employee_label,'') AS employee,l.debit,l.credit,l.currency_debit,l.currency_credit,
                    j.exchange_rate,l.line_no
                    FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id
                    JOIN accounts a ON a.id=l.account_id LEFT JOIN accounts parent ON parent.id=a.parent_id
                    LEFT JOIN stations s ON s.id=l.station_id LEFT JOIN cost_centers cc ON cc.id=l.cost_center_id
                    LEFT JOIN currencies c ON c.id=j.currency_id
                    LEFT JOIN receipt_headers rh ON j.source_type='CollectionReceipt' AND rh.id=j.source_id
                    LEFT JOIN currencies rc ON rc.id=rh.currency_id
                    WHERE """+' AND '.join(where)+" ORDER BY a.code,j.voucher_date,j.id,l.line_no",params).fetchall()
                account_meta={r['id']:dict(r) for r in db.execute("SELECT id,code,name,account_head_type,parent_id,account_level FROM accounts WHERE organization_id=? AND active=1 AND id IN ("+marks+") AND account_level=?",[org]+list(account_ids)+[level]).fetchall()}
                balances={key:0.0 for key in account_meta}; openings={key:0.0 for key in account_meta}; period_totals={key:{'debit':0.0,'credit':0.0} for key in account_meta}; detail_rows=[]
                def ledger_amount(row,side):
                    base=float(row[side] or 0)
                    if not currency: return base
                    amount=float(row['currency_'+side] or 0)
                    return amount if amount else base/max(float(row['exchange_rate'] or 1),0.0000001)
                for row in raw:
                    account_key=row['account_id']
                    if row['voucher_date']<from_date:
                        openings[account_key]+=ledger_amount(row,'debit')-ledger_amount(row,'credit')
                    else:
                        period_totals[account_key]['debit']+=ledger_amount(row,'debit')
                        period_totals[account_key]['credit']+=ledger_amount(row,'credit')
                        detail_rows.append((account_key,row))
                for key in balances: balances[key]=openings[key]
                rows=[]
                if view=='summary':
                    for key,meta in account_meta.items():
                        period=period_totals[key]
                        closing=openings[key]+period['debit']-period['credit']
                        if not show_zero and not any(round(float(x),2)!=0 for x in (openings[key],period['debit'],period['credit'],closing)): continue
                        rows.append({'account_id':key,'account_code':meta['code'] or '', 'account_title':meta['name'],'account_head_code':'','account_head_title':meta['account_head_type'] or '', 'voucher_date':'','voucher_no':'Summary','voucher_type':'','description':'Account period summary','station':'','cost_center':'','employee':'','currency':currency or 'PKR','opening_balance':round(openings[key],2),'debit':round(period['debit'],2),'credit':round(period['credit'],2),'balance':round(closing,2)})
                else:
                    for key,row in detail_rows:
                        meta=account_meta.get(key)
                        if meta is None: continue
                        debit=ledger_amount(row,'debit');credit=ledger_amount(row,'credit');balances[key]+=debit-credit
                        rows.append({'account_id':key,'account_code':row['account_code'] or '', 'account_title':row['account_title'],'account_head_code':row['account_head_code'],'account_head_title':row['account_head_title'] or row['account_head_type'],'voucher_date':row['voucher_date'],'voucher_no':row['voucher_no'],'voucher_type':row['voucher_type'],'description':row['description'],'station':row['station'],'cost_center':row['cost_center'],'employee':row['employee'],'currency':currency or row['transaction_currency'],'opening_balance':round(openings[key],2),'debit':round(debit,2),'credit':round(credit,2),'balance':round(balances[key],2)})
                    if show_zero:
                        present={r['account_id'] for r in rows}
                        for key,meta in account_meta.items():
                            if key not in present and key in openings:
                                rows.append({'account_id':key,'account_code':meta['code'] or '', 'account_title':meta['name'],'account_head_code':'','account_head_title':meta['account_head_type'] or '', 'voucher_date':'','voucher_no':'','voucher_type':'','description':'No posted transactions in range','station':'','cost_center':'','employee':'','currency':currency or 'PKR','opening_balance':round(openings[key],2),'debit':0,'credit':0,'balance':round(openings[key],2)})
                totals={'opening_balance':round(sum(openings.values()),2),'debit':round(sum(v['debit'] for v in period_totals.values()),2),'credit':round(sum(v['credit'] for v in period_totals.values()),2)}
                totals['closing_balance']=round(totals['opening_balance']+totals['debit']-totals['credit'],2)
                return self.send_json(200,{'account':dict(selected),'from_date':from_date,'to_date':to_date,'currency':currency or 'PKR','view':view,'rows':rows,'totals':totals,'demo_only':True})
            if path == '/api/reports/bank-balance':
                query=parse_qs(urlparse(self.path).query)
                try:
                    from_date=date.fromisoformat(query.get('from',['2026-10-01'])[0]).isoformat()
                    to_date=date.fromisoformat(query.get('to',['2026-10-31'])[0]).isoformat()
                    station=query.get('station_id',[''])[0] or None
                    cost_center=query.get('cost_center_id',[''])[0] or None
                    if station: int(station)
                    if cost_center: int(cost_center)
                except (ValueError,TypeError): return self.send_json(400,{'error':'Check the date and filter values.'})
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                show_cash=query.get('cash',['0'])[0]=='1';show_bank=query.get('bank',['0'])[0]=='1'
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                rows=[]
                accounts=db.execute("SELECT id,code,name,account_head_type FROM accounts WHERE organization_id=? AND account_level=4 AND active=1 AND account_type='asset' AND (lower(name) LIKE '%cash%' OR lower(name) LIKE '%bank%') ORDER BY code",(org,)).fetchall()
                for account in accounts:
                    kind=('Cash' if account['code']=='1100' else 'Bank' if account['code']=='1110' else 'Cheques In Hand' if account['code']=='1120' else (account['account_head_type'] or 'Other').title())
                    if (show_cash or show_bank) and not ((show_cash and kind=='Cash') or (show_bank and kind=='Bank')): continue
                    clauses=['l.account_id=?','j.organization_id=?',"j.status='posted'",'j.voucher_date<=?'];args=[account['id'],org,to_date]
                    if station: clauses.append('l.station_id=?');args.append(int(station))
                    if cost_center: clauses.append('l.cost_center_id=?');args.append(int(cost_center))
                    totals=db.execute("SELECT COALESCE(SUM(CASE WHEN j.voucher_date<? THEN l.debit-l.credit ELSE 0 END),0) opening,COALESCE(SUM(CASE WHEN j.voucher_date>=? THEN l.debit ELSE 0 END),0) receipts,COALESCE(SUM(CASE WHEN j.voucher_date>=? THEN l.credit ELSE 0 END),0) payments FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id WHERE "+' AND '.join(clauses),[from_date,from_date,from_date,*args]).fetchone()
                    opening=float(totals['opening']);receipts=float(totals['receipts']);payments=float(totals['payments'])
                    rows.append({'bank_type':kind or 'Other','designated_to':'Demo Company','detail_code':account['code'] or '','detail_title':account['name'],'opening':round(opening,2),'receipts':round(receipts,2),'payments':round(payments,2),'closing':round(opening+receipts-payments,2)})
                summary={key:round(sum(float(row[key]) for row in rows),2) for key in ('opening','receipts','payments','closing')}
                return self.send_json(200,{'rows':rows,'totals':summary,'from_date':from_date,'to_date':to_date,'demo_only':True})
            if path == '/api/reports/expense-report':
                query=parse_qs(urlparse(self.path).query)
                try:
                    from_date=date.fromisoformat(query.get('from',['2026-10-01'])[0]).isoformat()
                    to_date=date.fromisoformat(query.get('to',['2026-10-31'])[0]).isoformat()
                    station=query.get('station_id',[''])[0] or None
                    cost_center=query.get('cost_center_id',[''])[0] or None
                    account_id=query.get('account_id',[''])[0] or None
                    if station: int(station)
                    if cost_center: int(cost_center)
                    if account_id: int(account_id)
                except (ValueError,TypeError): return self.send_json(400,{'error':'Check the date and filter values.'})
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                view=query.get('view',['detail'])[0]
                if view not in ('detail','summary'): return self.send_json(400,{'error':'Choose Detail or Summary view.'})
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                clauses=['j.organization_id=?',"j.status='posted'",'j.voucher_date>=?','j.voucher_date<=?','a.account_type=\'expense\'','a.account_level=4']
                args=[org,from_date,to_date]
                if station: clauses.append('l.station_id=?');args.append(int(station))
                if cost_center: clauses.append('l.cost_center_id=?');args.append(int(cost_center))
                if account_id: clauses.append('a.id=?');args.append(int(account_id))
                if query.get('currency',[''])[0]: clauses.append('cur.code=?');args.append(query['currency'][0])
                base="""SELECT j.voucher_date AS date,COALESCE(head.code,'') AS account_head_code,COALESCE(head.name,'') AS account_head_title,
                    COALESCE(a.code,'') AS detail_account_code,a.name AS detail_account,j.voucher_no,j.voucher_type,l.description,
                    j.reference_no,COALESCE(s.name,'') AS station,COALESCE(cc.name,'') AS cost_center,COALESCE(cur.code,'PKR') AS currency,
                    j.exchange_rate AS currency_rate,(l.debit-l.credit) AS amount,
                    CASE WHEN l.currency_debit!=0 OR l.currency_credit!=0 THEN l.currency_debit-l.currency_credit
                         ELSE (l.debit-l.credit)/COALESCE(NULLIF(j.exchange_rate,0),1) END AS currency_amount
                    FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id
                    LEFT JOIN accounts head ON head.id=a.parent_id LEFT JOIN stations s ON s.id=l.station_id
                    LEFT JOIN cost_centers cc ON cc.id=l.cost_center_id LEFT JOIN currencies cur ON cur.id=j.currency_id WHERE """+' AND '.join(clauses)
                if view=='detail':
                    rows=db.execute(base+' ORDER BY j.voucher_date,j.voucher_no,l.line_no',args).fetchall()
                else:
                    rows=db.execute("SELECT MIN(date) AS date,account_head_code,account_head_title,detail_account_code,detail_account,'' AS voucher_no,'Summary' AS voucher_type,'Expense account period summary' AS description,'' AS reference_no,station,cost_center,currency,1 AS currency_rate,SUM(amount) AS amount,SUM(currency_amount) AS currency_amount FROM ("+base+") GROUP BY account_head_code,account_head_title,detail_account_code,detail_account,station,cost_center,currency ORDER BY detail_account_code",args).fetchall()
                result=[dict(r) | {'amount':round(float(r['amount']),2),'currency_amount':round(float(r['currency_amount']),2),'currency_rate':round(float(r['currency_rate']),6)} for r in rows]
                return self.send_json(200,{'rows':result,'totals':{'amount':round(sum(r['amount'] for r in result),2),'currency_amount':round(sum(r['currency_amount'] for r in result),2)},'from_date':from_date,'to_date':to_date,'view':view,'demo_only':True})
            if path == '/api/reports/receipt-report':
                query=parse_qs(urlparse(self.path).query)
                try:
                    from_date=date.fromisoformat(query.get('from',['2026-10-01'])[0]).isoformat()
                    to_date=date.fromisoformat(query.get('to',['2026-10-31'])[0]).isoformat()
                    station=query.get('station_id',[''])[0] or None
                    cost_center=query.get('cost_center_id',[''])[0] or None
                    account_id=query.get('account_id',[''])[0] or None
                    if station: int(station)
                    if cost_center: int(cost_center)
                    if account_id: int(account_id)
                except (ValueError,TypeError): return self.send_json(400,{'error':'Check the date and filter values.'})
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                view=query.get('view',['detail'])[0]
                if view not in ('detail','summary'): return self.send_json(400,{'error':'Choose Detail or Summary view.'})
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                clauses=['j.organization_id=?',"j.status='posted'",'j.voucher_date>=?','j.voucher_date<=?','l.customer_id IS NOT NULL',"j.source_type IN ('CollectionReceipt','ChequeClear','ChequeBounce','ChequeReplacementAfterBounce')"]
                args=[org,from_date,to_date]
                if station: clauses.append('l.station_id=?');args.append(int(station))
                if cost_center: clauses.append('l.cost_center_id=?');args.append(int(cost_center))
                if account_id: clauses.append('l.account_id=?');args.append(int(account_id))
                if query.get('employee',[''])[0]: clauses.append('l.employee_label=?');args.append(query['employee'][0])
                base="""SELECT j.voucher_date AS date,j.voucher_no AS doc_no,COALESCE(head.name,a.account_head_type,'') AS account_head,
                    COALESCE(cu.display_name,'') AS customer,COALESCE(l.employee_label,'') AS employee,COALESCE(l.description,'') AS description,
                    COALESCE(j.reference_no,l.instrument_no,'') AS reference_no,COALESCE(s.name,'') AS station,COALESCE(cc.name,'') AS cost_center,
                    (l.credit-l.debit) AS amount
                    FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id
                    LEFT JOIN accounts head ON head.id=a.parent_id LEFT JOIN customers cu ON cu.id=l.customer_id
                    LEFT JOIN stations s ON s.id=l.station_id LEFT JOIN cost_centers cc ON cc.id=l.cost_center_id WHERE """+' AND '.join(clauses)
                if view=='summary':
                    rows=db.execute("SELECT MIN(date) AS date,'' AS doc_no,account_head,customer,employee,'Receipt period summary' AS description,'' AS reference_no,station,cost_center,SUM(amount) AS amount FROM ("+base+") GROUP BY account_head,customer,employee,station,cost_center ORDER BY customer",args).fetchall()
                else: rows=db.execute(base+' ORDER BY j.voucher_date,j.voucher_no,l.line_no',args).fetchall()
                result=[dict(r) | {'amount':round(float(r['amount']),2),'company':'Demo Company'} for r in rows]
                return self.send_json(200,{'rows':result,'totals':{'amount':round(sum(r['amount'] for r in result),2)},'from_date':from_date,'to_date':to_date,'view':view,'demo_only':True})
            if path == '/api/reports/all-transactions':
                query=parse_qs(urlparse(self.path).query)
                try:
                    from_date=date.fromisoformat(query.get('from',['2026-10-01'])[0]).isoformat()
                    to_date=date.fromisoformat(query.get('to',['2026-10-31'])[0]).isoformat()
                    station=query.get('station_id',[''])[0] or None
                    cost_center=query.get('cost_center_id',[''])[0] or None
                    if station: int(station)
                    if cost_center: int(cost_center)
                except (ValueError,TypeError): return self.send_json(400,{'error':'Check the date and filter values.'})
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                clauses=['j.organization_id=?',"j.status='posted'",'j.voucher_date>=?','j.voucher_date<=?']
                args=[org,from_date,to_date]
                if station: clauses.append('l.station_id=?');args.append(int(station))
                if cost_center: clauses.append('l.cost_center_id=?');args.append(int(cost_center))
                currency=query.get('currency',[''])[0]
                if currency: clauses.append('cur.code=?');args.append(currency)
                voucher_type=query.get('voucher_type',[''])[0]
                if voucher_type and voucher_type!='All': clauses.append('j.voucher_type=?');args.append(voucher_type)
                account_ids=[v for value in query.get('account_ids',[]) for v in value.split(',') if v]
                if account_ids:
                    try: account_ids=list(dict.fromkeys(int(v) for v in account_ids))
                    except ValueError: return self.send_json(400,{'error':'The selected account filter is invalid.'})
                    clauses.append('l.account_id IN ('+','.join('?' for _ in account_ids)+')');args.extend(account_ids)
                rows=db.execute("""SELECT j.voucher_type,j.voucher_no,j.voucher_date,COALESCE(a.account_head_type,'') account_type,
                    a.name title,COALESCE(l.description,'') detail,l.debit,l.credit,COALESCE(cur.code,'PKR') currency
                    FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id
                    LEFT JOIN currencies cur ON cur.id=j.currency_id WHERE """+' AND '.join(clauses)+
                    " ORDER BY j.voucher_date,j.voucher_no,l.line_no",args).fetchall()
                result=[{'voucher_type':r['voucher_type'],'voucher_no':r['voucher_no'],'voucher_date':r['voucher_date'],'account_type':r['account_type'],'title':r['title'],'detail':r['detail'],'debit':round(float(r['debit']),2),'credit':round(float(r['credit']),2),'currency':r['currency']} for r in rows]
                return self.send_json(200,{'rows':result,'totals':{'debit':round(sum(r['debit'] for r in result),2),'credit':round(sum(r['credit'] for r in result),2)},'from_date':from_date,'to_date':to_date,'demo_only':True})
            if path == '/api/reports/cash-bank-statement':
                query=parse_qs(urlparse(self.path).query)
                try:
                    from_date=date.fromisoformat(query.get('from',['2026-10-01'])[0]).isoformat()
                    to_date=date.fromisoformat(query.get('to',['2026-10-31'])[0]).isoformat()
                    station=query.get('station_id',[''])[0] or None
                    cost_center=query.get('cost_center_id',[''])[0] or None
                    if station: int(station)
                    if cost_center: int(cost_center)
                except (ValueError,TypeError): return self.send_json(400,{'error':'Check the date and filter values.'})
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                show_cash=query.get('cash',['1'])[0]=='1';show_bank=query.get('bank',['1'])[0]=='1'
                view=query.get('view',['detail'])[0]
                if view not in ('detail','summary'): return self.send_json(400,{'error':'Choose Detail or Summary view.'})
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                account_filter=query.get('account_id',[])
                accounts=db.execute("SELECT id,code,name FROM accounts WHERE organization_id=? AND account_level=4 AND active=1 AND account_type='asset' AND (lower(name) LIKE '%cash%' OR lower(name) LIKE '%bank%') ORDER BY code",(org,)).fetchall()
                allowed={str(a['id']):a for a in accounts}
                selected=set(account_filter)&set(allowed)
                if account_filter: accounts=[a for a in accounts if str(a['id']) in selected]
                rows=[]
                for account in accounts:
                    kind='Cash' if account['code']=='1100' or 'cash' in account['name'].lower() else 'Bank'
                    if not ((kind=='Cash' and show_cash) or (kind=='Bank' and show_bank)): continue
                    filters=['l.account_id=?','j.organization_id=?',"j.status='posted'",'j.voucher_date<=?']
                    args=[account['id'],org,to_date]
                    if station: filters.append('l.station_id=?');args.append(int(station))
                    if cost_center: filters.append('l.cost_center_id=?');args.append(int(cost_center))
                    opening=db.execute("SELECT COALESCE(SUM(l.debit-l.credit),0) FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id WHERE "+' AND '.join(filters[:-1])+" AND j.voucher_date<?",[account['id'],org,from_date,*args[3:]]).fetchone()[0]
                    if view=='detail':
                        detail=db.execute("SELECT j.voucher_date,j.voucher_no,j.voucher_type,l.description,l.debit,l.credit FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id WHERE "+' AND '.join(filters)+" AND j.voucher_date>=? ORDER BY j.voucher_date,j.voucher_no,l.line_no",[*args,from_date]).fetchall()
                        for line in detail:
                            rows.append({'account_id':account['id'],'account_title':account['name'],'account_code':account['code'] or '', 'date':line['voucher_date'],'document_no':line['voucher_no'],'voucher_type':line['voucher_type'],'description':line['description'],'debit':round(float(line['debit']),2),'credit':round(float(line['credit']),2)})
                    else:
                        period=db.execute("SELECT COALESCE(SUM(l.debit),0) debit,COALESCE(SUM(l.credit),0) credit FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id WHERE "+' AND '.join(filters)+" AND j.voucher_date>=?",[*args,from_date]).fetchone()
                        rows.append({'account_id':account['id'],'account_title':account['name'],'account_code':account['code'] or '', 'opening':round(float(opening),2),'debit':round(float(period['debit']),2),'credit':round(float(period['credit']),2),'closing':round(float(opening)+float(period['debit'])-float(period['credit']),2)})
                totals={'debit':round(sum(r['debit'] for r in rows),2),'credit':round(sum(r['credit'] for r in rows),2)}
                if view=='summary': totals.update({'opening':round(sum(r['opening'] for r in rows),2),'closing':round(sum(r['closing'] for r in rows),2)})
                return self.send_json(200,{'rows':rows,'totals':totals,'from_date':from_date,'to_date':to_date,'view':view,'demo_only':True})
            if path == '/api/reports/ledger':
                query=parse_qs(urlparse(self.path).query)
                account_id=query.get('detail_account_id',[None])[0]
                if not account_id: return self.send_json(400,{'error':'Select a detail account to view its ledger.'})
                from_date=date.fromisoformat(query.get('from',[date.today().replace(day=1).isoformat()])[0]).isoformat()
                to_date=date.fromisoformat(query.get('to',[date.today().isoformat()])[0]).isoformat()
                if from_date>to_date: return self.send_json(400,{'error':'From date must be on or before To date.'})
                station=query.get('station_id',[None])[0] or None; cost_center=query.get('cost_center_id',[None])[0] or None
                if station is not None: station=int(station)
                if cost_center is not None: cost_center=int(cost_center)
                currency=query.get('currency',[None])[0] or None
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                account=db.execute("SELECT id,code,name,account_level FROM accounts WHERE id=? AND organization_id=? AND active=1",(int(account_id),org)).fetchone()
                if account is None or account['account_level']!=4: return self.send_json(400,{'error':'Choose an active transaction account.'})
                filter_sql=" AND (? IS NULL OR l.station_id=?) AND (? IS NULL OR l.cost_center_id=?)"
                opening=db.execute("SELECT COALESCE(SUM(CASE WHEN ? IS NULL THEN l.debit-l.credit ELSE (CASE WHEN l.currency_debit>0 THEN l.currency_debit ELSE l.debit/COALESCE(NULLIF(j.exchange_rate,0),1) END)-(CASE WHEN l.currency_credit>0 THEN l.currency_credit ELSE l.credit/COALESCE(NULLIF(j.exchange_rate,0),1) END) END),0) FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id LEFT JOIN currencies c ON c.id=j.currency_id LEFT JOIN receipt_headers rh ON j.source_type='CollectionReceipt' AND rh.id=j.source_id LEFT JOIN currencies rc ON rc.id=rh.currency_id WHERE l.account_id=? AND j.organization_id=? AND j.status='posted' AND j.voucher_date<?"+filter_sql+" AND (? IS NULL OR COALESCE(c.code,rc.code,'PKR')=?)",(currency,account['id'],org,from_date,station,station,cost_center,cost_center,currency,currency)).fetchone()[0]
                raw=db.execute("""SELECT j.voucher_date,j.voucher_no,l.instrument_no,l.instrument_date,
                    COALESCE(s.name,'') AS station,COALESCE(cc.name,'') AS cost_center,
                    COALESCE(l.description,'') AS description,COALESCE(c.code,rc.code,'PKR') AS currency,
                    CASE WHEN ? IS NULL THEN l.debit WHEN l.currency_debit>0 THEN l.currency_debit ELSE l.debit/COALESCE(NULLIF(j.exchange_rate,0),1) END AS debit,
                    CASE WHEN ? IS NULL THEN l.credit WHEN l.currency_credit>0 THEN l.currency_credit ELSE l.credit/COALESCE(NULLIF(j.exchange_rate,0),1) END AS credit,
                    l.currency_debit,l.currency_credit
                    FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id
                    LEFT JOIN currencies c ON c.id=j.currency_id LEFT JOIN receipt_headers rh ON j.source_type='CollectionReceipt' AND rh.id=j.source_id
                    LEFT JOIN currencies rc ON rc.id=rh.currency_id LEFT JOIN stations s ON s.id=l.station_id
                    LEFT JOIN cost_centers cc ON cc.id=l.cost_center_id
                    WHERE l.account_id=? AND j.organization_id=? AND j.status='posted' AND j.voucher_date>=? AND j.voucher_date<=?"""+filter_sql+" AND (? IS NULL OR COALESCE(c.code,rc.code,'PKR')=?) ORDER BY j.voucher_date,j.id,l.line_no",
                    (currency,currency,account['id'],org,from_date,to_date,station,station,cost_center,cost_center,currency,currency)).fetchall()
                transactions=[dict(r) for r in raw]
                if query.get('view',['detail'])[0]=='summary':
                    grouped={}
                    for row in transactions:
                        key=(row['voucher_date'],row['currency'])
                        item=grouped.setdefault(key,{'voucher_date':row['voucher_date'],'voucher_no':'Summary','instrument_no':'','instrument_date':None,'station':row['station'],'cost_center':row['cost_center'],'description':'Daily ledger summary','currency':row['currency'],'debit':0.0,'credit':0.0,'currency_debit':0.0,'currency_credit':0.0})
                        for field in ('debit','credit','currency_debit','currency_credit'): item[field]+=float(row[field] or 0)
                    transactions=list(grouped.values())
                balance=float(opening or 0); debit_total=credit_total=0.0
                for row in transactions:
                    row['debit']=round(float(row['debit'] or 0),2);row['credit']=round(float(row['credit'] or 0),2)
                    debit_total+=row['debit'];credit_total+=row['credit'];balance+=row['debit']-row['credit']
                    row['balance']=round(balance,2)
                account_info={'id':account['id'],'code':account['code'],'name':account['name']}
                totals={'opening_balance':round(float(opening or 0),2),'debit':round(debit_total,2),'credit':round(credit_total,2),'closing_balance':round(balance,2)}
                return self.send_json(200,{'account':account_info,'from_date':from_date,'to_date':to_date,'currency':currency or 'All','totals':totals,'rows':transactions})
            if path.startswith('/api/customers/') and path.endswith('/statement'):
                customer_id=int(path.strip('/').split('/')[2]); query=parse_qs(urlparse(self.path).query)
                from_date=query.get('from',[None])[0]; to_date=query.get('to',[None])[0]
                sql="SELECT customer_id,customer_name,entry_date,document_no,source_type,description,debit,credit,running_balance FROM v_customer_statement_lines WHERE customer_id=?"
                params=[customer_id]
                if from_date: sql+=" AND entry_date>=?"; params.append(date.fromisoformat(from_date).isoformat())
                if to_date: sql+=" AND entry_date<=?"; params.append(date.fromisoformat(to_date).isoformat())
                sql+=" ORDER BY entry_date,document_no"
                return self.send_json(200,[dict(r) for r in db.execute(sql,params).fetchall()])
        self.send_json(404,{'error':'Not found'})
    def do_PUT(self):
        path=urlparse(self.path).path
        try:
            receipt_id=int(path.rsplit('/',1)[1]); body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
            receipt_date=date.fromisoformat(body['receipt_date']); lines=body.get('lines',[]); deposits=body.get('deposits',[])
            if not lines or not deposits: raise ValueError('Receipt needs detail and deposit lines.')
            currency_type=str(body.get('currency_type','buying')).lower(); rate=float(body.get('exchange_rate',1))
            if currency_type not in ('buying','selling') or rate<=0: raise ValueError('Choose a valid currency type and positive currency rate.')
            normalized=[]; customer_ids=set()
            for line in lines:
                customer=int(line['customer_id']); gross=round(float(line['gross_amount']),2); discount=round(float(line.get('discount_amount',0)),2); inv_rate=float(line.get('invoice_rate',1)); method=str(line.get('payment_type','other')).lower()
                if gross<=0 or discount<0 or discount>gross or inv_rate<=0: raise ValueError('Check gross, discount and invoice rate values.')
                if method not in ('cash','cheque','bank_transfer','other'): raise ValueError('Choose a valid receipt type.')
                normalized.append((customer,line.get('station_id') or None,line.get('cost_center_id') or None,str(line.get('employee_label','')),str(line.get('comment','')),str(line.get('invoice_no','')),inv_rate,method,gross,discount,str(line.get('instrument_no','')),line.get('instrument_date') or None)); customer_ids.add(customer)
            net=round(sum(x[8]-x[9] for x in normalized),2)
            if net<=0: raise ValueError('Receipt net must be greater than zero.')
            if any(round(float(d['amount']),2)<=0 for d in deposits): raise ValueError('Every deposit split must be greater than zero.')
            if round(sum(float(d['amount']) for d in deposits)-net,2)!=0: raise ValueError('Deposit split total must equal receipt net total.')
            customer_net={}
            for line in normalized: customer_net[line[0]]=round(customer_net.get(line[0],0)+line[8]-line[9],2)
            deposit_net={}
            for deposit in deposits:
                allocated=deposit.get('customer_id') or (next(iter(customer_ids)) if len(customer_ids)==1 else None)
                if allocated is None or int(allocated) not in customer_ids: raise ValueError('Allocate every deposit split to a receipt customer.')
                deposit['customer_id']=int(allocated)
                deposit_net[int(allocated)]=round(deposit_net.get(int(allocated),0)+float(deposit['amount']),2)
            if any(round(deposit_net.get(customer,0)-amount,2)!=0 for customer,amount in customer_net.items()): raise ValueError('Each customer’s deposit allocation must equal that customer’s net receipt amount.')
            with connect() as db:
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                db.execute('BEGIN IMMEDIATE')
                header=db.execute("SELECT * FROM receipt_headers WHERE id=? AND organization_id=?",(receipt_id,org)).fetchone()
                if header is None: return self.send_json(404,{'error':'Receipt not found'})
                if header['status']!='draft': return self.send_json(409,{'error':'Only draft receipts can be edited.'})
                if db.execute("SELECT 1 FROM journal_headers WHERE source_type='CollectionReceipt' AND source_id=?",(receipt_id,)).fetchone(): return self.send_json(409,{'error':'A receipt linked to a journal cannot be edited.'})
                cheque_rows=db.execute("SELECT id FROM cheques WHERE receipt_id=?",(receipt_id,)).fetchall()
                for cheque in cheque_rows:
                    if db.execute("SELECT 1 FROM cheque_events WHERE cheque_id=? AND event_type<>'received'",(cheque['id'],)).fetchone() or db.execute("SELECT 1 FROM cheques WHERE replacement_of_id=?",(cheque['id'],)).fetchone():
                        return self.send_json(409,{'error':'This receipt has cheque lifecycle history and cannot be edited safely.'})
                cur=db.execute("SELECT id FROM currencies WHERE code=?",(body['currency'],)).fetchone()
                if cur is None: raise ValueError('Choose a supported currency.')
                validate_receipt_invoice_allocations(db,org,[{'customer_id':x[0],'gross_amount':x[8],'invoice_no':x[5]} for x in normalized],cur['id'])
                for line in normalized:
                    if db.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=? AND active=1",(line[0],org)).fetchone() is None: raise ValueError('Choose a valid active customer for every line.')
                    if line[1] and db.execute("SELECT 1 FROM stations WHERE id=? AND organization_id=?",(int(line[1]),org)).fetchone() is None: raise ValueError('Choose a valid station.')
                    if line[2] and db.execute("SELECT 1 FROM cost_centers WHERE id=? AND organization_id=?",(int(line[2]),org)).fetchone() is None: raise ValueError('Choose a valid cost center.')
                db.execute("DELETE FROM cheque_events WHERE cheque_id IN (SELECT id FROM cheques WHERE receipt_id=?)",(receipt_id,)); db.execute("DELETE FROM cheques WHERE receipt_id=?",(receipt_id,)); db.execute("DELETE FROM deposit_splits WHERE receipt_id=?",(receipt_id,)); db.execute("DELETE FROM receipt_lines WHERE receipt_id=?",(receipt_id,))
                db.execute("UPDATE receipt_headers SET receipt_date=?,currency_id=?,currency_type=?,exchange_rate=?,call_label=?,cheque_type=?,locker=?,remarks=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(receipt_date.isoformat(),cur['id'],currency_type,rate,str(body.get('call_label','')),str(body.get('cheque_type','')),str(body.get('locker','')),str(body.get('remarks','')),receipt_id))
                for i,x in enumerate(normalized,1):
                    db.execute("INSERT INTO receipt_lines(receipt_id,line_no,customer_id,station_id,cost_center_id,employee_label,comment,invoice_no,invoice_rate,payment_type,gross_amount,gross_secondary,discount_amount,discount_secondary,instrument_no,instrument_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(receipt_id,i,x[0],x[1],x[2],x[3],x[4],x[5],x[6],x[7],x[8],round(x[8]*rate,2),x[9],round(x[9]*rate,2),x[10],x[11]))
                for i,d in enumerate(deposits,1):
                    amount=round(float(d['amount']),2); account_id=int(d['account_id']); method=d.get('method','cash')
                    account=db.execute("SELECT id FROM accounts WHERE id=? AND organization_id=? AND active=1 AND account_type='asset' AND account_level=4",(account_id,org)).fetchone()
                    if account is None or amount<0: raise ValueError('Choose a valid active deposit account and nonnegative amount.')
                    if method not in ('cash','cheque','bank_transfer','other'): raise ValueError('Choose a valid deposit type.')
                    validate_cheque_deposit_account(db,org,method,account_id)
                    if method=='cheque' and (amount<=0 or not str(d.get('instrument_no','')).strip() or not d.get('instrument_date') or not str(d.get('bank_label','')).strip()): raise ValueError('Cheque deposits need a positive amount, cheque number, date and bank.')
                    for key,table in [('station_id','stations'),('cost_center_id','cost_centers')]:
                        if d.get(key) and db.execute(f"SELECT 1 FROM {table} WHERE id=? AND organization_id=?",(int(d[key]),org)).fetchone() is None: raise ValueError('Choose valid deposit dimensions.')
                    db.execute("INSERT INTO deposit_splits(receipt_id,line_no,account_id,customer_id,method,reference,bank_label,instrument_no,instrument_date,currency_date,station_id,cost_center_id,amount,amount_secondary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(receipt_id,i,account_id,d.get('customer_id'),method,str(d.get('reference','')),str(d.get('bank_label','')),str(d.get('instrument_no','')),d.get('instrument_date') or None,d.get('currency_date') or None,d.get('station_id') or None,d.get('cost_center_id') or None,amount,round(amount*rate,2)))
                    split_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    if method=='cheque':
                        db.execute("INSERT INTO cheques(organization_id,receipt_id,deposit_split_id,cheque_no,cheque_date,currency_date,signatory,payer_bank,customer_id,currency_id,currency_rate,amount,base_amount,status,locker,remarks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'in_hand',?,?)",(org,receipt_id,split_id,d['instrument_no'],d['instrument_date'],d.get('currency_date') or None,str(d.get('signatory','')),d['bank_label'],d['customer_id'],cur['id'],rate,amount,amount*rate,str(body.get('locker','')),str(d.get('reference',''))))
                        cheque_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]; db.execute("INSERT INTO cheque_events(cheque_id,event_type,from_status,to_status,actor_id) VALUES (?,'received',NULL,'in_hand',1)",(cheque_id,))
                db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'updated','receipt',?,'Local demo draft receipt updated')",(org,receipt_id))
                db.commit()
            return self.send_json(200,{'id':receipt_id,'receipt_no':header['receipt_no'],'status':'draft'})
        except (ValueError,KeyError,TypeError,json.JSONDecodeError,sqlite3.IntegrityError) as exc: return self.send_json(400,{'error':str(exc)})
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
            if path.startswith('/api/receipts/') and path.endswith('/workflow'):
                receipt_id=int(path.split('/')[-2]); action=str(body.get('action','')).lower()
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                    db.execute('BEGIN IMMEDIATE')
                    header=db.execute("SELECT h.*,c.code AS currency FROM receipt_headers h JOIN currencies c ON c.id=h.currency_id WHERE h.id=? AND h.organization_id=?",(receipt_id,org)).fetchone()
                    if header is None: return self.send_json(404,{'error':'Receipt not found'})
                    transitions={'submit':('draft','pending','submitted'),'approve':('pending','approved','approved'),'post':('approved','posted','posted'),'void':('draft','void','voided')}
                    if action not in transitions: raise ValueError('Choose submit, approve or post.')
                    expected,next_status,event_action=transitions[action]
                    if header['status']!=expected: return self.send_json(409,{'error':f"Receipt must be {expected} before this action."})
                    voucher_no=None
                    if action=='void' and db.execute("SELECT 1 FROM cheques WHERE receipt_id=? AND status IN ('in_hand','deposited','transferred','presented') LIMIT 1",(receipt_id,)).fetchone():
                        return self.send_json(409,{'error':'Return or resolve cheques that are still in custody before voiding this receipt.'})
                    if action=='submit':
                        for split in db.execute("SELECT * FROM deposit_splits WHERE receipt_id=? AND method='cheque'",(receipt_id,)).fetchall():
                            validate_cheque_deposit_account(db,org,split['method'],split['account_id'])
                            if not db.execute("SELECT 1 FROM cheques WHERE deposit_split_id=? AND status IN ('in_hand','deposited','transferred','presented') LIMIT 1",(split['id'],)).fetchone():
                                raise ValueError(f"Cheque deposit line {split['line_no']} has no cheque in custody. Register a replacement before submitting this receipt.")
                    if action=='post':
                        if db.execute("SELECT 1 FROM journal_headers WHERE source_type='CollectionReceipt' AND source_id=?",(receipt_id,)).fetchone():
                            return self.send_json(409,{'error':'This receipt already has a journal.'})
                        lines=db.execute("SELECT l.*,cu.account_id FROM receipt_lines l JOIN customers cu ON cu.id=l.customer_id WHERE l.receipt_id=? ORDER BY l.line_no",(receipt_id,)).fetchall()
                        deposits=db.execute("SELECT * FROM deposit_splits WHERE receipt_id=? ORDER BY line_no",(receipt_id,)).fetchall()
                        if not lines or not deposits: raise ValueError('Receipt needs detail and deposit lines before posting.')
                        validate_receipt_invoice_allocations(db,org,lines,header['currency_id'])
                        gross=round(sum(float(x['gross_amount']) for x in lines),2); discount=round(sum(float(x['discount_amount']) for x in lines),2); net=round(gross-discount,2)
                        if net<=0: raise ValueError('Receipt net must be greater than zero before posting.')
                        if any(round(float(x['amount']),2)<=0 for x in deposits): raise ValueError('Every deposit split must be greater than zero.')
                        if round(sum(float(x['amount']) for x in deposits)-net,2)!=0: raise ValueError('Deposit split total must equal receipt net before posting.')
                        for split in deposits:
                            validate_cheque_deposit_account(db,org,split['method'],split['account_id'])
                            if split['method']=='cheque' and not db.execute("SELECT 1 FROM cheques WHERE deposit_split_id=? AND status IN ('in_hand','deposited','transferred','presented') LIMIT 1",(split['id'],)).fetchone():
                                raise ValueError(f"Cheque deposit line {split['line_no']} has no cheque in custody. Register a replacement before posting this receipt.")
                        for line in lines:
                            account=db.execute("SELECT id FROM accounts WHERE id=? AND organization_id=? AND active=1 AND account_type='asset' AND account_level=4",(line['account_id'],org)).fetchone()
                            if account is None: raise ValueError('Every customer must have an active receivable transaction account.')
                        discount_account=None
                        if discount:
                            discount_account=db.execute("SELECT id FROM accounts WHERE organization_id=? AND active=1 AND account_level=4 AND (code='4090' OR lower(name) LIKE '%discount%') ORDER BY CASE WHEN code='4090' THEN 0 ELSE 1 END LIMIT 1",(org,)).fetchone()
                            if discount_account is None: raise ValueError('Add an active discount expense transaction account before posting discounts.')
                        currency=db.execute("SELECT id FROM currencies WHERE code=?",(header['currency'],)).fetchone()
                        seq=db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(voucher_no,4) AS INTEGER)),7700)+1 FROM journal_headers WHERE organization_id=? AND voucher_no GLOB 'VR-[0-9]*'",(org,)).fetchone()[0]
                        voucher_no=f'VR-{seq:05d}'
                        db.execute("INSERT INTO journal_headers(organization_id,voucher_no,voucher_date,voucher_type,reference_no,currency_id,exchange_rate,source_type,source_id,status,created_by,posted_at) VALUES (?,?,?,?,?,?,?,'CollectionReceipt',?,'posted',1,CURRENT_TIMESTAMP)",(org,voucher_no,header['receipt_date'],'CR',header['receipt_no'],currency['id'],header['exchange_rate'],receipt_id))
                        journal_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]; line_no=0; rate=float(header['exchange_rate'])
                        for split in deposits:
                            amount=round(float(split['amount']),2); line_no+=1
                            db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,station_id,cost_center_id,instrument_no,instrument_date,description,debit,currency_debit) VALUES (?,?,?,?,?,?,?,?,?,?)",(journal_id,line_no,split['account_id'],split['station_id'],split['cost_center_id'],split['instrument_no'],split['instrument_date'],f"Receipt deposit · {split['method']}",round(amount*rate,2),amount))
                        for line in lines:
                            if float(line['discount_amount']):
                                amount=round(float(line['discount_amount']),2); line_no+=1
                                db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,customer_id,station_id,cost_center_id,employee_label,description,debit,currency_debit) VALUES (?,?,?,?,?,?,?,?,?,?)",(journal_id,line_no,discount_account['id'],line['customer_id'],line['station_id'],line['cost_center_id'],line['employee_label'],'Receipt discount',round(amount*rate,2),amount))
                            amount=round(float(line['gross_amount']),2); line_no+=1
                            db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,customer_id,station_id,cost_center_id,employee_label,description,credit,currency_credit) VALUES (?,?,?,?,?,?,?,?,?,?)",(journal_id,line_no,line['account_id'],line['customer_id'],line['station_id'],line['cost_center_id'],line['employee_label'],line['comment'] or 'Customer receipt',round(amount*rate,2),amount))
                        totals=db.execute("SELECT ROUND(SUM(debit),2) debit,ROUND(SUM(credit),2) credit FROM journal_lines WHERE journal_id=?",(journal_id,)).fetchone()
                        if totals['debit']!=totals['credit']: raise ValueError('Receipt journal is out of balance; posting was cancelled.')
                    db.execute("UPDATE receipt_headers SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(next_status,receipt_id))
                    db.execute("INSERT INTO approval_events(entity_type,entity_id,action,actor_id,note) VALUES ('receipt',?,?,1,?)",(receipt_id,event_action,str(body.get('note',''))))
                    db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,?,'receipt',?,?)",(org,action,receipt_id,f'Collection Receipt {action}'))
                    db.commit()
                    return self.send_json(200,{'id':receipt_id,'status':next_status,'voucher_no':voucher_no})
            if path == '/api/journals':
                voucher_date=date.fromisoformat(body['voucher_date']).isoformat(); currency=body.get('currency','PKR'); rate=float(body.get('exchange_rate',1)); lines=body.get('lines',[])
                if rate<=0: raise ValueError('Currency rate must be greater than zero.')
                if len(lines)<2: raise ValueError('A voucher needs at least two account lines.')
                normalized=[]; debit_total=credit_total=0.0
                for line in lines:
                    account_id=int(line['account_id']); debit_currency=round(float(line.get('debit',0)),2); credit_currency=round(float(line.get('credit',0)),2)
                    if (debit_currency>0)==(credit_currency>0): raise ValueError('Each line must have either a debit or a credit amount.')
                    debit=round(debit_currency*rate,2); credit=round(credit_currency*rate,2)
                    debit_total+=debit; credit_total+=credit
                    normalized.append((account_id,line.get('customer_id') or None,line.get('station_id') or None,line.get('cost_center_id') or None,str(line.get('employee_label','')),str(line.get('instrument_no','')),line.get('instrument_date') or None,str(line.get('description','')),debit,credit,debit_currency,credit_currency))
                if round(debit_total-credit_total,2)!=0: raise ValueError('Voucher is out of balance. Debit and credit totals must match in base currency.')
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                    currency_row=db.execute("SELECT id FROM currencies WHERE code=?",(currency,)).fetchone()
                    if currency_row is None: raise ValueError('Choose a supported currency.')
                    db.execute('BEGIN IMMEDIATE')
                    for line in normalized:
                        account=db.execute("SELECT id,account_level,account_type,account_head_type FROM accounts WHERE id=? AND organization_id=? AND active=1",(line[0],org)).fetchone()
                        if account is None or account['account_level']!=4: raise ValueError('Choose an active transaction account on every line.')
                        if account['account_head_type']=='Customer' and line[1] is None: raise ValueError('Choose a customer for the customer control account.')
                        if line[1] is not None and db.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=? AND active=1",(int(line[1]),org)).fetchone() is None:
                            raise ValueError('Choose a valid customer on the account line.')
                    seq=db.execute("SELECT COALESCE(MAX(source_id),0)+1 FROM journal_headers WHERE organization_id=? AND source_type='Voucher'",(org,)).fetchone()[0]
                    voucher_no=f"{body.get('voucher_type','JV')}-{seq:05d}"
                    db.execute("INSERT INTO journal_headers(organization_id,voucher_no,voucher_date,voucher_type,reference_no,currency_id,exchange_rate,source_type,source_id,status,created_by,posted_at) VALUES (?,?,?,?,?,?,?,'Voucher',?,'posted',1,CURRENT_TIMESTAMP)",(org,voucher_no,voucher_date,body.get('voucher_type','JV'),body.get('reference_no',''),currency_row['id'],rate,seq))
                    journal_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    for i,line in enumerate(normalized,1): db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,customer_id,station_id,cost_center_id,employee_label,instrument_no,instrument_date,description,debit,credit,currency_debit,currency_credit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(journal_id,i,*line))
                    db.execute("INSERT INTO approval_events(entity_type,entity_id,action,actor_id,note) VALUES ('journal',?,'posted',1,'Demo voucher posted')",(journal_id,))
                    db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'posted','journal',?,'Voucher posted in local demo')",(org,journal_id))
                return self.send_json(201,{'id':journal_id,'voucher_no':voucher_no,'status':'posted','debit_total':round(debit_total,2)})
            if path.startswith('/api/cheques/') and path.endswith('/events'):
                parts=path.strip('/').split('/')
                cheque_id=int(parts[2]); action=body.get('action'); reason=str(body.get('reason','')).strip()
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                    cheque=db.execute("SELECT * FROM cheques WHERE id=? AND organization_id=?",(cheque_id,org)).fetchone()
                    if cheque is None: raise ValueError('Cheque not found.')
                    db.execute('BEGIN IMMEDIATE')
                    transitions={'in_hand':{'deposit':'deposited','return':'returned','transfer':'transferred'},'deposited':{'present':'presented','bounce':'bounced','return':'returned','transfer':'transferred'},'presented':{'clear':'cleared','bounce':'bounced'},'bounced':{'physical_return':'returned'},'transferred':{'deposit':'deposited','present':'presented','bounce':'bounced','return':'returned'}}
                    if action=='replace':
                        if cheque['status'] not in ('bounced','returned'): raise ValueError('Only a bounced or returned cheque can be replaced.')
                        if db.execute("SELECT 1 FROM cheques WHERE replacement_of_id=?",(cheque_id,)).fetchone(): raise ValueError('A replacement cheque is already linked to this cheque.')
                        new_no=str(body.get('cheque_no','')).strip(); new_date=date.fromisoformat(body['cheque_date']).isoformat(); new_bank=str(body.get('payer_bank',cheque['payer_bank'])).strip(); currency_date=date.fromisoformat(body['currency_date']).isoformat() if body.get('currency_date') else None; signatory=str(body.get('signatory','')).strip()
                        if not new_no or not new_bank: raise ValueError('Replacement cheque number and bank are required.')
                        bounce_journal=db.execute("SELECT id FROM journal_headers WHERE source_type IN ('ChequeBounce','ChequeReturn') AND source_id=? AND status='posted' ORDER BY id DESC LIMIT 1",(cheque_id,)).fetchone()
                        db.execute("INSERT INTO cheques(organization_id,receipt_id,deposit_split_id,replacement_of_id,cheque_no,cheque_date,currency_date,signatory,payer_bank,customer_id,currency_id,currency_rate,amount,base_amount,status,locker,remarks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'in_hand',?,?)",(org,cheque['receipt_id'],cheque['deposit_split_id'],cheque_id,new_no,new_date,currency_date,signatory,new_bank,cheque['customer_id'],cheque['currency_id'],cheque['currency_rate'],cheque['amount'],cheque['base_amount'],cheque['locker'],reason))
                        replacement_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                        replacement=db.execute("SELECT * FROM cheques WHERE id=?",(replacement_id,)).fetchone()
                        voucher=None
                        if bounce_journal: voucher=post_cheque_lifecycle_journal(db,org,replacement,'replace')['voucher_no']
                        db.execute("INSERT INTO cheque_events(cheque_id,event_type,from_status,to_status,reason,reference,actor_id) VALUES (?,'replacement_created',?,?,?, ?,1)",(cheque_id,cheque['status'],cheque['status'],reason,new_no))
                        db.execute("INSERT INTO cheque_events(cheque_id,event_type,from_status,to_status,reason,reference,actor_id) VALUES (?,'received',NULL,'in_hand',?, ?,1)",(replacement_id,f'Replacement for cheque {cheque_id}',str(cheque_id)))
                        db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'replacement_created','cheque',?,?)",(org,replacement_id,f'Replacement for cheque {cheque_id}'))
                        return self.send_json(201,{'id':replacement_id,'status':'in_hand','replacement_of_id':cheque_id,'voucher_no':voucher})
                    next_status=transitions.get(cheque['status'],{}).get(action)
                    if next_status is None: raise ValueError(f"Action '{action}' is not valid for a {cheque['status']} cheque.")
                    if action=='bounce' and not reason: raise ValueError('Enter the bank return reason for a bounced cheque.')
                    voucher=None
                    if action in ('clear','bounce','return') and action!='physical_return':
                        receipt_posted=db.execute("SELECT 1 FROM journal_headers WHERE organization_id=? AND source_type='CollectionReceipt' AND source_id=? AND status='posted'",(org,cheque['receipt_id'])).fetchone()
                        if action in ('clear','bounce') and not receipt_posted: raise ValueError('Post the collection receipt before clearing or bouncing its cheque.')
                        if receipt_posted: voucher=post_cheque_lifecycle_journal(db,org,cheque,action)['voucher_no']
                    db.execute("UPDATE cheques SET status=?,remarks=CASE WHEN ?='' THEN remarks ELSE ? END WHERE id=?",(next_status,reason,reason,cheque_id))
                    db.execute("INSERT INTO cheque_events(cheque_id,event_type,from_status,to_status,reason,actor_id) VALUES (?,?,?,?,?,1)",(cheque_id,'physical_return' if action in ('physical_return','return') else next_status,cheque['status'],next_status,reason))
                    if action in ('clear','bounce','return') and voucher:
                        db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,?,'cheque',?,?)",(org,action,cheque_id,f'Cheque {action} journal {voucher}'))
                    return self.send_json(200,{'id':cheque_id,'status':next_status,'voucher_no':voucher})
            if path == '/api/payments':
                payment_date=date.fromisoformat(body['payment_date']).isoformat(); rate=round(float(body.get('exchange_rate',1)),10); lines=body.get('lines',[]); splits=body.get('splits',[])
                if rate<=0: raise ValueError('Currency rate must be greater than zero.')
                if not lines or not splits: raise ValueError('Add payee and payment source details.')
                normalized=[]
                for line in lines:
                    gross=round(float(line.get('gross_amount',0)),2); discount=round(float(line.get('discount_amount',0)),2); account_id=int(line['account_id'])
                    if gross<=0 or discount<0 or discount>gross: raise ValueError('Gross amount must be positive and discount cannot exceed gross.')
                    method=str(line.get('payment_type','other')).lower()
                    if method not in ('cash','cheque','bank_transfer','other'): raise ValueError('Choose a valid payee payment type.')
                    normalized.append((account_id,int(line['customer_id']) if line.get('customer_id') else None,line.get('station_id') or None,line.get('cost_center_id') or None,str(line.get('employee_label','')),str(line.get('comment','')),str(line.get('invoice_no','')),method,str(line.get('instrument_no','')),line.get('instrument_date') or None,gross,discount))
                net=round(sum(row[10]-row[11] for row in normalized),2)
                if net<=0: raise ValueError('Payment net must be greater than zero.')
                if any(round(float(item.get('amount',0)),2)<=0 for item in splits) or round(sum(float(item.get('amount',0)) for item in splits)-net,2)!=0: raise ValueError('Payment source total must equal the payment net total.')
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                    cur=db.execute("SELECT id FROM currencies WHERE code=?",(body.get('currency'),)).fetchone()
                    if cur is None: raise ValueError('Choose a supported currency.')
                    db.execute('BEGIN IMMEDIATE')
                    for row in normalized:
                        account=db.execute("SELECT id,account_head_type FROM accounts WHERE id=? AND organization_id=? AND active=1 AND account_level=4",(row[0],org)).fetchone()
                        if account is None: raise ValueError('Choose an active transaction account for each payee line.')
                        if account['account_head_type']=='Customer' and row[1] is None: raise ValueError('Choose a customer for the customer account line.')
                        if row[1] and db.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=? AND active=1",(row[1],org)).fetchone() is None: raise ValueError('Choose a valid customer.')
                        for value,table in [(row[2],'stations'),(row[3],'cost_centers')]:
                            if value and db.execute(f"SELECT 1 FROM {table} WHERE id=? AND organization_id=?",(int(value),org)).fetchone() is None: raise ValueError('Choose valid station and cost center values.')
                    for item in splits:
                        source=db.execute("SELECT id FROM accounts WHERE id=? AND organization_id=? AND active=1 AND account_level=4 AND account_type='asset'",(int(item['account_id']),org)).fetchone()
                        if source is None: raise ValueError('Choose an active cash or bank transaction account for each payment source.')
                        method=str(item.get('method','other')).lower()
                        if method not in ('cash','cheque','bank_transfer','other'): raise ValueError('Choose a valid payment source type.')
                        if method=='cheque' and (not str(item.get('cheque_book','')).strip() or not str(item.get('instrument_no','')).strip() or not item.get('instrument_date')): raise ValueError('Cheque payments need a cheque book, number and date.')
                    seq=db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(payment_no,5) AS INTEGER)),0)+1 FROM payment_headers WHERE organization_id=?",(org,)).fetchone()[0]; payment_no=f'PAY-{seq:05d}'
                    db.execute("INSERT INTO payment_headers(organization_id,payment_no,payment_date,currency_id,exchange_rate,remarks,status,created_by) VALUES (?,?,?,?,?,?,'draft',1)",(org,payment_no,payment_date,cur['id'],rate,str(body.get('remarks',''))))
                    payment_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    for i,row in enumerate(normalized,1): db.execute("INSERT INTO payment_lines(payment_id,line_no,account_id,customer_id,station_id,cost_center_id,employee_label,comment,invoice_no,payment_type,instrument_no,instrument_date,gross_amount,discount_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(payment_id,i,*row))
                    for i,item in enumerate(splits,1): db.execute("INSERT INTO payment_splits(payment_id,line_no,account_id,method,cheque_book,reference,instrument_no,instrument_date,station_id,cost_center_id,amount) VALUES (?,?,?,?,?,?,?,?,?,?,?)",(payment_id,i,int(item['account_id']),str(item.get('method','other')).lower(),str(item.get('cheque_book','')),str(item.get('reference','')),str(item.get('instrument_no','')),item.get('instrument_date') or None,item.get('station_id') or None,item.get('cost_center_id') or None,round(float(item['amount']),2)))
                    db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'created','payment',?,'Fictional local payment saved as draft')",(org,payment_id)); db.commit()
                return self.send_json(201,{'id':payment_id,'payment_no':payment_no,'status':'draft'})
            if path.startswith('/api/payments/') and path.endswith('/post'):
                try: payment_id=int(path.split('/')[-2])
                except ValueError: return self.send_json(404,{'error':'Payment not found'})
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]; db.execute('BEGIN IMMEDIATE')
                    header=db.execute("SELECT h.*,c.code AS currency FROM payment_headers h JOIN currencies c ON c.id=h.currency_id WHERE h.id=? AND h.organization_id=?",(payment_id,org)).fetchone()
                    if header is None: return self.send_json(404,{'error':'Payment not found'})
                    if header['status']!='draft': return self.send_json(409,{'error':'Only draft payments can be posted.'})
                    lines=db.execute("SELECT l.* FROM payment_lines l JOIN accounts a ON a.id=l.account_id WHERE l.payment_id=? ORDER BY l.line_no",(payment_id,)).fetchall(); splits=db.execute("SELECT * FROM payment_splits WHERE payment_id=? ORDER BY line_no",(payment_id,)).fetchall()
                    if not lines or not splits: raise ValueError('Payment needs payee and source details before posting.')
                    gross=round(sum(float(row['gross_amount']) for row in lines),2); discount=round(sum(float(row['discount_amount']) for row in lines),2); net=round(gross-discount,2)
                    if net<=0 or round(sum(float(row['amount']) for row in splits)-net,2)!=0: raise ValueError('Payment sources must equal the payment net before posting.')
                    discount_account=db.execute("SELECT id FROM accounts WHERE organization_id=? AND active=1 AND account_level=4 AND code='4095' AND account_type='income'",(org,)).fetchone() if discount else None
                    if discount and discount_account is None: raise ValueError('Add an active Discount Received income transaction account before posting a discounted payment.')
                    seq=db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(voucher_no,4) AS INTEGER)),0)+1 FROM journal_headers WHERE organization_id=? AND voucher_no GLOB 'PV-[0-9]*'",(org,)).fetchone()[0]; voucher_no=f'PV-{seq:05d}'
                    db.execute("INSERT INTO journal_headers(organization_id,voucher_no,voucher_date,voucher_type,reference_no,currency_id,exchange_rate,source_type,source_id,status,created_by,posted_at) VALUES (?,?,?,'PV',?,?,?,'Payment',?,'posted',1,CURRENT_TIMESTAMP)",(org,voucher_no,header['payment_date'],header['payment_no'],header['currency_id'],header['exchange_rate'],payment_id))
                    journal_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]; line_no=0; fx=float(header['exchange_rate'])
                    for split in splits:
                        line_no+=1; amount=round(float(split['amount']),2)
                        db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,station_id,cost_center_id,instrument_no,instrument_date,description,credit,currency_credit) VALUES (?,?,?,?,?,?,?,?,?,?)",(journal_id,line_no,split['account_id'],split['station_id'],split['cost_center_id'],split['instrument_no'],split['instrument_date'],f"Payment source · {split['method']}",round(amount*fx,2),amount))
                    for row in lines:
                        amount=round(float(row['gross_amount']),2); line_no+=1
                        db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,customer_id,station_id,cost_center_id,employee_label,instrument_no,instrument_date,description,debit,currency_debit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(journal_id,line_no,row['account_id'],row['customer_id'],row['station_id'],row['cost_center_id'],row['employee_label'],row['instrument_no'],row['instrument_date'],row['comment'] or 'Payment to account',round(amount*fx,2),amount))
                        if float(row['discount_amount']):
                            amount=round(float(row['discount_amount']),2); line_no+=1
                            db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,description,credit,currency_credit) VALUES (?,?,?,'Payment discount received',?,?)",(journal_id,line_no,discount_account['id'],round(amount*fx,2),amount))
                    totals=db.execute("SELECT ROUND(SUM(debit),2) debit,ROUND(SUM(credit),2) credit FROM journal_lines WHERE journal_id=?",(journal_id,)).fetchone()
                    if totals['debit']!=totals['credit']: raise ValueError('Payment journal is out of balance; posting was cancelled.')
                    db.execute("UPDATE payment_headers SET status='posted',updated_at=CURRENT_TIMESTAMP WHERE id=?",(payment_id,))
                    db.execute("INSERT INTO approval_events(entity_type,entity_id,action,actor_id,note) VALUES ('payment',?,'posted',1,'Payment posted')",(payment_id,))
                    db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'posted','payment',?,?)",(org,payment_id,f'Payment posted as {voucher_no}'))
                    db.commit()
                    return self.send_json(200,{'id':payment_id,'payment_no':header['payment_no'],'voucher_no':voucher_no,'status':'posted'})
            if path == '/api/expenses':
                expense_date=date.fromisoformat(body['expense_date']).isoformat(); payment_type=str(body.get('payment_type','cash')).lower(); rate=round(float(body.get('exchange_rate',1)),10); lines=body.get('lines',[])
                if payment_type not in ('cash','bank') or rate<=0: raise ValueError('Choose a valid cash/bank type and positive currency rate.')
                if not lines: raise ValueError('Add at least one expense detail line.')
                normalized=[]
                for line in lines:
                    amount=round(float(line.get('currency_amount',0)),2); account_id=int(line['account_id'])
                    if amount<=0: raise ValueError('Every expense line must have a positive amount.')
                    normalized.append((account_id,str(line.get('description','')),str(line.get('reference','')),line.get('station_id') or None,line.get('cost_center_id') or None,str(line.get('employee_label','')),amount,round(amount*rate,2)))
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]; cur=db.execute("SELECT id FROM currencies WHERE code=?",(body.get('currency'),)).fetchone()
                    if cur is None: raise ValueError('Choose a supported currency.')
                    source=db.execute("SELECT id FROM accounts WHERE id=? AND organization_id=? AND active=1 AND account_type='asset' AND account_level=4",(int(body['from_account_id']),org)).fetchone()
                    if source is None: raise ValueError('Choose an active cash or bank transaction account.')
                    db.execute('BEGIN IMMEDIATE')
                    for row in normalized:
                        account=db.execute("SELECT id FROM accounts WHERE id=? AND organization_id=? AND active=1 AND account_type='expense' AND account_level=4",(row[0],org)).fetchone()
                        if account is None: raise ValueError('Choose an active expense transaction account for every line.')
                        for value,table in [(row[3],'stations'),(row[4],'cost_centers')]:
                            if value and db.execute(f"SELECT 1 FROM {table} WHERE id=? AND organization_id=?",(int(value),org)).fetchone() is None: raise ValueError('Choose valid station and cost center values.')
                    seq=db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(expense_no,5) AS INTEGER)),0)+1 FROM expense_headers WHERE organization_id=?",(org,)).fetchone()[0]; expense_no=f'EXP-{seq:05d}'
                    db.execute("INSERT INTO expense_headers(organization_id,expense_no,expense_date,reference_no,payment_type,from_account_id,instrument_no,currency_id,exchange_rate,remarks,status,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,'draft',1)",(org,expense_no,expense_date,str(body.get('reference_no','')),payment_type,int(body['from_account_id']),str(body.get('instrument_no','')),cur['id'],rate,str(body.get('remarks',''))))
                    expense_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    for i,row in enumerate(normalized,1): db.execute("INSERT INTO expense_lines(expense_id,line_no,account_id,description,reference,station_id,cost_center_id,employee_label,currency_amount,base_amount) VALUES (?,?,?,?,?,?,?,?,?,?)",(expense_id,i,*row))
                    db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'created','expense',?,'Fictional local expense saved as draft')",(org,expense_id)); db.commit()
                return self.send_json(201,{'id':expense_id,'expense_no':expense_no,'status':'draft'})
            if path.startswith('/api/expenses/'):
                try: expense_id=int(path.split('/')[-2]); action=str(path.split('/')[-1]).lower()
                except (ValueError,IndexError): return self.send_json(404,{'error':'Expense not found'})
                transitions={'submit':('draft','pending','submitted'),'approve':('pending','approved','approved'),'reject':('pending','rejected','rejected'),'post':('approved','posted','posted')}
                if action not in transitions: return self.send_json(404,{'error':'Expense action not found'})
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]; db.execute('BEGIN IMMEDIATE')
                    header=db.execute("SELECT h.*,c.code AS currency FROM expense_headers h JOIN currencies c ON c.id=h.currency_id WHERE h.id=? AND h.organization_id=?",(expense_id,org)).fetchone()
                    if header is None: return self.send_json(404,{'error':'Expense not found'})
                    expected,next_status,event=transitions[action]
                    if header['status']!=expected: return self.send_json(409,{'error':f"Expense must be {expected} before this action."})
                    voucher_no=None
                    if action=='post':
                        lines=db.execute("SELECT * FROM expense_lines WHERE expense_id=? ORDER BY line_no",(expense_id,)).fetchall()
                        if not lines: raise ValueError('Expense needs detail lines before posting.')
                        total=round(sum(float(line['base_amount']) for line in lines),2); amount=round(sum(float(line['currency_amount']) for line in lines),2)
                        seq=db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(voucher_no,4) AS INTEGER)),0)+1 FROM journal_headers WHERE organization_id=? AND voucher_no GLOB 'EX-[0-9]*'",(org,)).fetchone()[0]; voucher_no=f'EX-{seq:05d}'
                        db.execute("INSERT INTO journal_headers(organization_id,voucher_no,voucher_date,voucher_type,reference_no,currency_id,exchange_rate,source_type,source_id,status,created_by,posted_at) VALUES (?,?,?,'EX',?,?,?,'Expense',?,'posted',1,CURRENT_TIMESTAMP)",(org,voucher_no,header['expense_date'],header['expense_no'],header['currency_id'],header['exchange_rate'],expense_id))
                        journal_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                        for i,line in enumerate(lines,1): db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,station_id,cost_center_id,employee_label,instrument_no,instrument_date,description,debit,currency_debit) VALUES (?,?,?,?,?,?,?,?,?,?,?)",(journal_id,i,line['account_id'],line['station_id'],line['cost_center_id'],line['employee_label'],header['instrument_no'],header['expense_date'],line['description'] or line['reference'] or 'Expense',line['base_amount'],line['currency_amount']))
                        db.execute("INSERT INTO journal_lines(journal_id,line_no,account_id,instrument_no,instrument_date,description,credit,currency_credit) VALUES (?,?,?,?,?,?,?,?)",(journal_id,len(lines)+1,header['from_account_id'],header['instrument_no'],header['expense_date'],f"Expense paid from {header['payment_type']} account",total,amount))
                        totals=db.execute("SELECT ROUND(SUM(debit),2) debit,ROUND(SUM(credit),2) credit FROM journal_lines WHERE journal_id=?",(journal_id,)).fetchone()
                        if totals['debit']!=totals['credit']: raise ValueError('Expense journal is out of balance; posting was cancelled.')
                    db.execute("UPDATE expense_headers SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(next_status,expense_id))
                    db.execute("INSERT INTO approval_events(entity_type,entity_id,action,actor_id,note) VALUES ('expense',?,?,1,?)",(expense_id,event,str(body.get('note',''))))
                    db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,?,'expense',?,?)",(org,action,expense_id,f'Expense {action}'))
                    db.commit()
                    return self.send_json(200,{'id':expense_id,'expense_no':header['expense_no'],'status':next_status,'voucher_no':voucher_no})
            if path == '/api/accounts':
                code=str(body.get('code','')).strip() or None; name=str(body.get('name','')).strip(); typ=body.get('account_type')
                if not name: raise ValueError('Account name is required.')
                if typ not in ('asset','liability','equity','income','expense'): raise ValueError('Choose an account type.')
                with connect() as db:
                    org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                    parent_id=body.get('parent_id') or None
                    level=1
                    if parent_id:
                        parent=db.execute("SELECT account_level,account_type FROM accounts WHERE id=? AND organization_id=?",(int(parent_id),org)).fetchone()
                        if parent is None: raise ValueError('Choose a valid parent account.')
                        if parent['account_level']>=4: raise ValueError('Transaction accounts cannot contain child accounts.')
                        level=parent['account_level']+1
                        typ=parent['account_type']
                    if level != 3 and not code: raise ValueError('Account code is required for this hierarchy level.')
                    db.execute("INSERT INTO accounts(organization_id,code,name,parent_id,account_type,account_level,account_head_type,account_note,is_control) VALUES (?,?,?,?,?,?,?,?,?)",(org,code,name,parent_id,typ,level,str(body.get('account_head_type','')),str(body.get('account_note','')),1 if level<4 else 0))
                    account_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    return self.send_json(201,{'id':account_id,'code':code,'name':name,'parent_id':parent_id,'account_type':typ,'account_level':level})
            if path != '/api/receipts': return self.send_json(404,{'error':'Not found'})
            receipt_date=date.fromisoformat(body['receipt_date'])
            lines=body.get('lines',[]); deposits=body.get('deposits',[])
            if not lines: raise ValueError('Add at least one receipt detail line.')
            if not deposits: raise ValueError('Add at least one deposit detail.')
            currency_type=str(body.get('currency_type','buying')).lower()
            if currency_type not in ('buying','selling'): raise ValueError('Choose a valid currency type.')
            header_rate=float(body.get('exchange_rate',1))
            if header_rate<=0: raise ValueError('Currency rate must be greater than zero.')
            normalized=[]
            customer_ids=set()
            for line in lines:
                gross=round(float(line['gross_amount']),2); discount=round(float(line.get('discount_amount',0)),2)
                if gross<=0 or discount<0 or discount>gross: raise ValueError('Gross must be positive and discount cannot exceed gross.')
                rate=float(line.get('invoice_rate',1))
                if rate<=0: raise ValueError('Invoice rate must be greater than zero.')
                pay_type=str(line.get('payment_type','other')).lower()
                if pay_type not in ('cash','cheque','bank_transfer','other'): raise ValueError('Choose a valid receipt type.')
                normalized.append((int(line['customer_id']),line.get('station_id') or None,line.get('cost_center_id') or None,str(line.get('employee_label','')),gross,discount,pay_type,line.get('instrument_no',''),line.get('instrument_date') or None,line.get('invoice_no',''),rate,str(line.get('comment',''))))
                customer_ids.add(int(line['customer_id']))
            net=round(sum(x[4]-x[5] for x in normalized),2)
            if net<=0: raise ValueError('Receipt net must be greater than zero.')
            if any(round(float(x.get('amount',0)),2)<=0 for x in deposits): raise ValueError('Every deposit split must be greater than zero.')
            deposit_total=round(sum(float(x.get('amount',0)) for x in deposits),2)
            if not deposits or round(deposit_total-net,2)!=0: raise ValueError('Deposit split total must equal receipt net total.')
            customer_net={}
            for line in normalized: customer_net[line[0]]=round(customer_net.get(line[0],0)+line[4]-line[5],2)
            deposit_net={}
            for item in deposits:
                allocated=item.get('customer_id') or (next(iter(customer_ids)) if len(customer_ids)==1 else None)
                if allocated is None or int(allocated) not in customer_ids: raise ValueError('Allocate every deposit split to a receipt customer.')
                item['customer_id']=int(allocated)
                deposit_net[int(allocated)]=round(deposit_net.get(int(allocated),0)+float(item['amount']),2)
            if any(round(deposit_net.get(customer,0)-amount,2)!=0 for customer,amount in customer_net.items()): raise ValueError('Each customer’s deposit allocation must equal that customer’s net receipt amount.')
            with connect() as db:
                org=db.execute("SELECT id FROM organizations WHERE code='DEMO'").fetchone()[0]
                db.execute('BEGIN IMMEDIATE')
                cur=db.execute("SELECT id FROM currencies WHERE code=?",(body['currency'],)).fetchone()
                if cur is None: raise ValueError('Choose a supported currency.')
                validate_receipt_invoice_allocations(db,org,[{'customer_id':x[0],'gross_amount':x[4],'invoice_no':x[9]} for x in normalized],cur['id'])
                for line in normalized:
                    if db.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=? AND active=1",(line[0],org)).fetchone() is None:
                        raise ValueError('Choose a valid active customer for each receipt line.')
                    if line[1] is not None and db.execute("SELECT 1 FROM stations WHERE id=? AND organization_id=?",(int(line[1]),org)).fetchone() is None:
                        raise ValueError('Choose a valid station for each receipt line.')
                    if line[2] is not None and db.execute("SELECT 1 FROM cost_centers WHERE id=? AND organization_id=?",(int(line[2]),org)).fetchone() is None:
                        raise ValueError('Choose a valid cost center for each receipt line.')
                seq=db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(receipt_no,5) AS INTEGER)),0)+1 FROM receipt_headers WHERE organization_id=?",(org,)).fetchone()[0]
                receipt_no=f'RCP-{seq:05d}'
                db.execute("INSERT INTO receipt_headers(organization_id,receipt_no,receipt_date,currency_id,currency_type,exchange_rate,call_label,cheque_type,locker,remarks,status,created_by) VALUES (?,?,?,?,?,?,?,?,?,?, 'draft',1)",(org,receipt_no,receipt_date.isoformat(),cur['id'],currency_type,header_rate,str(body.get('call_label','')),str(body.get('cheque_type','')),str(body.get('locker','')),body.get('remarks','')))
                rid=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                for i,line in enumerate(normalized,1):
                    customer_id,station_id,cost_center_id,employee,gross,discount,payment_type,instrument_no,instrument_date,invoice_no,rate,comment=line
                    db.execute("INSERT INTO receipt_lines(receipt_id,line_no,customer_id,station_id,cost_center_id,employee_label,comment,invoice_no,invoice_rate,payment_type,gross_amount,gross_secondary,discount_amount,discount_secondary,instrument_no,instrument_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,i,customer_id,station_id,cost_center_id,employee,comment,invoice_no,rate,payment_type,gross,round(gross*header_rate,2),discount,round(discount*header_rate,2),instrument_no,instrument_date))
                for i,item in enumerate(deposits,1):
                    amount=round(float(item['amount']),2)
                    if amount<=0: raise ValueError('Every deposit split must be greater than zero.')
                    account_id=int(item['account_id'])
                    deposit_account=db.execute("SELECT id,account_type,account_level FROM accounts WHERE id=? AND organization_id=? AND active=1",(account_id,org)).fetchone()
                    if deposit_account is None or deposit_account['account_type']!='asset' or deposit_account['account_level']!=4:
                        raise ValueError('Select a valid deposit account for every deposit line.')
                    method=item.get('method','cash')
                    if method not in ('cash','cheque','bank_transfer','other'): raise ValueError('Choose a valid deposit type.')
                    if method=='cheque' and amount<=0: raise ValueError('Cheque amount must be greater than zero.')
                    validate_cheque_deposit_account(db,org,method,account_id)
                    if method=='cheque' and (not str(item.get('instrument_no','')).strip() or not item.get('instrument_date') or not str(item.get('bank_label','')).strip()):
                        raise ValueError('Cheque deposits need a cheque number, cheque date and bank.')
                    if item.get('station_id') and db.execute("SELECT 1 FROM stations WHERE id=? AND organization_id=?",(int(item['station_id']),org)).fetchone() is None:
                        raise ValueError('Choose a valid station for each deposit line.')
                    if item.get('cost_center_id') and db.execute("SELECT 1 FROM cost_centers WHERE id=? AND organization_id=?",(int(item['cost_center_id']),org)).fetchone() is None:
                        raise ValueError('Choose a valid cost center for each deposit line.')
                    db.execute("INSERT INTO deposit_splits(receipt_id,line_no,account_id,customer_id,method,reference,bank_label,instrument_no,instrument_date,currency_date,station_id,cost_center_id,amount,amount_secondary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,i,account_id,item.get('customer_id'),method,item.get('reference',''),item.get('bank_label',''),item.get('instrument_no',''),item.get('instrument_date') or None,item.get('currency_date') or None,item.get('station_id') or None,item.get('cost_center_id') or None,amount,round(amount*header_rate,2)))
                    split_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    if method=='cheque':
                        db.execute("INSERT INTO cheques(organization_id,receipt_id,deposit_split_id,cheque_no,cheque_date,currency_date,signatory,payer_bank,customer_id,currency_id,currency_rate,amount,base_amount,status,locker,remarks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'in_hand',?,?)",(org,rid,split_id,item['instrument_no'],item['instrument_date'],item.get('currency_date') or None,str(item.get('signatory','')),item['bank_label'],item['customer_id'],cur['id'],header_rate,amount,amount*header_rate,str(body.get('locker','')),item.get('reference','')))
                        cheque_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                        db.execute("INSERT INTO cheque_events(cheque_id,event_type,from_status,to_status,actor_id) VALUES (?,'received',NULL,'in_hand',1)",(cheque_id,))
                db.execute("INSERT INTO audit_log(organization_id,actor_id,action,entity_type,entity_id,detail) VALUES (?,1,'created','receipt',?,'Local demo receipt created')",(org,rid))
                db.commit()
            return self.send_json(201,{'id':rid,'receipt_no':receipt_no,'status':'draft'})
        except (ValueError,KeyError,TypeError,json.JSONDecodeError,sqlite3.IntegrityError) as exc:
            return self.send_json(400,{'error':str(exc)})
    def log_message(self, fmt, *args):
        print(f'{self.address_string()} {fmt % args}')


if __name__ == '__main__':
    initialize()
    host='127.0.0.1'; port=int(os.environ.get('ERP_REPLICA_PORT','8002'))
    print(f'ERP replica API: http://{host}:{port} (database: {DB_PATH})')
    ThreadingHTTPServer((host,port),Handler).serve_forever()
