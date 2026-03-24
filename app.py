#!/usr/bin/env python3
"""
Poto Projects Financial Dashboard — Web App
────────────────────────────────────────────
Flask backend. Reads QuickBooks CSVs uploaded via the web UI,
serves the dashboard, and calls Claude for AI insights and Q&A.

Local usage:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 app.py

Then open http://localhost:5000
"""

import csv, json, os, re
from pathlib import Path
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import anthropic

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'poto-dashboard-2026-secret')

# ── Data directory: /data on Render (persistent disk), ./data locally ──────────
DATA_DIR   = Path(os.environ.get('DATA_DIR', Path(__file__).parent / 'data'))
UPLOADS_DIR = DATA_DIR / 'uploads'
HISTORY_JSON = DATA_DIR / 'financial-history.json'
POTO_DATA_JSON = DATA_DIR / 'poto-data.json'

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ── Claude client ───────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))

# ── Month mappings ──────────────────────────────────────────────────────────────
MONTH_ABBR = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12
}
MONTH_NAME = {
    1:'January',2:'February',3:'March',4:'April',5:'May',6:'June',
    7:'July',8:'August',9:'September',10:'October',11:'November',12:'December'
}
MONTH_SHORT = {
    1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',
    7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'
}

# ── Chart colors ────────────────────────────────────────────────────────────────
CATEGORY_COLORS = {
    'Contractors':        '#1e5c3a',
    'Office & Software':  '#3b82f6',
    'Travel':             '#ef4444',
    'Meals':              '#f59e0b',
    'Taxes':              '#f97316',
    'Legal & Accounting': '#8b5cf6',
    'Memberships & Subs': '#06b6d4',
    'Business Licenses':  '#ec4899',
    'Bank & QB Fees':     '#84cc16',
}
FALLBACK_COLORS = ['#64748b','#94a3b8','#475569','#334155']

# ── Known contractor/client metadata ────────────────────────────────────────────
CONTRACTOR_META = {
    'Chengda Cai': {
        'type': 'Contract',
        'contracts': [
            {'client': 'Jeremy Lin',   'start': 'Nov 1, 2025',  'end': 'Apr 30, 2026'},
            {'client': 'Bobby Portis', 'start': 'Jan 13, 2026', 'end': 'Jun 13, 2026'},
        ],
    },
    'Richard Chen': {
        'type': 'Contract',
        'contracts': [
            {'client': '', 'start': 'Mar 9, 2026', 'end': 'Apr 9, 2026'},
        ],
    },
}

CLIENT_META = {
    'Patricia Lin': {
        'type': 'Retainer',
        'known_as': 'Jeremy Lin',
        'start': 'Jan 1, 2026',
        'end': None,
    },
    'Sierra Lord': {
        'type': 'Retainer',
        'known_as': 'Kindred Ventures',
        'start': None,
        'end': None,
    },
    '9 Star Entertainment LLC': {
        'type': 'Retainer',
        'known_as': 'Bobby Portis',
        'start': None,
        'end': None,
    },
}


# ── Helpers ─────────────────────────────────────────────────────────────────────
def parse_amount(s):
    if not s: return 0.0
    s = str(s).replace('$','').replace(',','').replace('"','').strip()
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return float(s)
    except:
        return 0.0

def fmt(n):
    if n < 0:
        return f"-${abs(n):,.2f}"
    return f"${n:,.2f}"


# ── CSV parsers (same logic as generate-dashboard.py) ───────────────────────────
def parse_profit_loss(filepath):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))
    kv = {}
    for row in rows[4:]:
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            kv[row[0].strip()] = parse_amount(row[1])

    def get(*keys):
        for k in keys:
            if k in kv: return kv[k]
        return 0.0

    meals  = get('Total for Meals') or (get('Meals') + get('Team meals') + get('Meals with clients'))
    travel = get('Total for Travel') or get('Travel')
    office = get('Total for Office expenses') or get('Office expenses')
    legal  = get('Total for Legal & accounting services') or get('Legal & accounting services') or get('Accounting fees')
    subs   = get('Memberships & subscriptions')
    taxes  = get('Taxes paid')
    bank   = get('Bank fees & service charges')
    biz    = get('Business licenses')
    qb     = get('QuickBooks Payments Fees')

    expense_cats = {
        'Meals':              round(meals, 2),
        'Travel':             round(travel, 2),
        'Office & Software':  round(office, 2),
        'Legal & Accounting': round(legal, 2),
        'Taxes':              round(taxes, 2),
        'Memberships & Subs': round(subs, 2),
        'Business Licenses':  round(biz, 2),
        'Bank & QB Fees':     round(bank + qb, 2),
    }
    expense_cats = {k: v for k, v in expense_cats.items() if v > 0}

    return {
        'income':               round(get('Total for Income'), 2),
        'cogs':                 round(get('Total for Cost of Goods Sold'), 2),
        'gross_profit':         round(get('Gross Profit'), 2),
        'total_expenses':       round(get('Total for Expenses'), 2),
        'net_operating_income': round(get('Net Operating Income'), 2),
        'net_income':           round(get('Net Income'), 2),
        'expense_categories':   expense_cats,
    }


def parse_transactions(filepath):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))
    header_idx = next(i for i, r in enumerate(rows) if r and r[0].strip() == 'Date')
    headers = [h.strip() for h in rows[header_idx]]

    def col(row, name):
        try: return row[headers.index(name)].strip()
        except: return ''

    contractors = {}
    charlotte_draw = 0.0
    eric_draw = 0.0

    for row in rows[header_idx+1:]:
        if not row or not row[0].strip() or row[0].strip().upper() == 'TOTAL':
            continue
        account = col(row, 'Account full name')
        name    = col(row, 'Name')
        amount  = abs(parse_amount(col(row, 'Amount')))
        if 'Contract Labor' in account:
            if name and name not in ('', 'QuickBooks Payments'):
                contractors[name] = round(contractors.get(name, 0.0) + amount, 2)
        if 'Charlotte Lao Draw' in account:
            charlotte_draw += amount
        if 'Jiaqi Yang Draw' in account or 'Eric Yang Draw' in account:
            eric_draw += amount

    return {
        'contractors':    contractors,
        'charlotte_draw': round(charlotte_draw, 2),
        'eric_draw':      round(eric_draw, 2),
    }


def parse_invoices(filepath):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))
    header_idx = next(i for i, r in enumerate(rows) if r and r[0].strip() == 'Date')
    headers = [h.strip() for h in rows[header_idx]]

    def col(row, name):
        try: return row[headers.index(name)].strip()
        except: return ''

    today = date.today()
    invoices = []
    for row in rows[header_idx+1:]:
        if not row or not row[0].strip() or row[0].strip().upper() == 'TOTAL':
            continue
        due_str      = col(row, 'Due date')
        amount       = parse_amount(col(row, 'Amount'))
        open_balance = parse_amount(col(row, 'Open balance'))
        due_date = None
        try:
            due_date = datetime.strptime(due_str, '%m/%d/%Y').date()
        except:
            pass
        days_overdue = 0
        if open_balance > 0 and due_date and today > due_date:
            days_overdue = (today - due_date).days
        invoices.append({
            'num':          col(row, 'Num'),
            'client':       col(row, 'Name'),
            'date':         col(row, 'Date'),
            'due_date':     due_str,
            'amount':       round(amount, 2),
            'open_balance': round(open_balance, 2),
            'status':       'Outstanding' if open_balance > 0 else 'Paid',
            'days_overdue': days_overdue,
        })
    return invoices


# ── File discovery ───────────────────────────────────────────────────────────────
def find_file(directory, pattern_parts, month_abbr, year):
    for f in directory.iterdir():
        if not f.is_file(): continue
        name_lower = f.name.lower()
        if all(p in name_lower for p in pattern_parts):
            if month_abbr in name_lower and str(year) in name_lower:
                return f
    return None

def find_months(directory):
    months = set()
    for f in directory.iterdir():
        m = re.search(r'(?:transactions|invoices|profit.loss)[_-]([a-z]{3})[_-](\d{4})', f.name.lower())
        if m:
            abbr, year = m.group(1), int(m.group(2))
            if abbr in MONTH_ABBR:
                months.add((year, MONTH_ABBR[abbr]))
    return sorted(months)


# ── Data helpers ─────────────────────────────────────────────────────────────────
def load_history():
    if HISTORY_JSON.exists():
        with open(HISTORY_JSON, 'r') as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(HISTORY_JSON, 'w') as f:
        json.dump(dict(sorted(history.items())), f, indent=2)

def load_poto_data():
    if POTO_DATA_JSON.exists():
        with open(POTO_DATA_JSON, 'r') as f:
            return json.load(f)
    return {'clients': {}, 'contractors': {}}

def save_poto_data(data):
    with open(POTO_DATA_JSON, 'w') as f:
        json.dump(data, f, indent=2)


# ── Process CSV uploads ──────────────────────────────────────────────────────────
def process_uploads_dir():
    """Parse all CSVs in uploads dir. Returns list of month dicts."""
    months = find_months(UPLOADS_DIR)
    results = []
    for year, month in months:
        abbr  = [k for k, v in MONTH_ABBR.items() if v == month][0]
        label = f"{MONTH_NAME[month]} {year}"
        key   = f"{year}-{month:02d}"
        pl_file  = find_file(UPLOADS_DIR, ['profit', 'loss'], abbr, year)
        tx_file  = find_file(UPLOADS_DIR, ['transaction'], abbr, year)
        inv_file = find_file(UPLOADS_DIR, ['invoice'], abbr, year)
        if not all([pl_file, tx_file, inv_file]):
            continue
        try:
            pl  = parse_profit_loss(pl_file)
            tx  = parse_transactions(tx_file)
            inv = parse_invoices(inv_file)
            results.append({
                'key':     key,
                'label':   label,
                'short':   f"{MONTH_SHORT[month]} {str(year)[2:]}",
                'year':    year,
                'month':   month,
                **pl,
                **tx,
                'invoices': inv,
            })
        except Exception as e:
            print(f"Error processing {label}: {e}")
    return results


# ── Build dashboard payload ──────────────────────────────────────────────────────
def build_dashboard_data():
    history   = load_history()
    poto_data = load_poto_data()

    if not history:
        return None

    sorted_keys = sorted(history.keys())
    months_data = [history[k] for k in sorted_keys]
    latest = months_data[-1]

    # Make sure each month has a 'short' label
    for i, (k, m) in enumerate(zip(sorted_keys, months_data)):
        if 'short' not in m:
            parts = k.split('-')
            if len(parts) == 2:
                mo = int(parts[1])
                yr = parts[0][2:]
                m['short'] = f"{MONTH_SHORT[mo]} {yr}"

    # Chart data
    chart_labels   = [m['short'] for m in months_data]
    chart_income   = [m['income'] for m in months_data]
    chart_expenses = [m['total_expenses'] + m.get('cogs', 0) for m in months_data]
    chart_net      = [m['net_income'] for m in months_data]

    # Donut chart (latest month)
    donut_cats = {'Contractors': round(latest.get('cogs', 0), 2)}
    donut_cats.update(latest.get('expense_categories', {}))
    donut_cats = {k: v for k, v in donut_cats.items() if v > 0}
    donut_colors = []
    fallback_idx = 0
    for cat in donut_cats:
        if cat in CATEGORY_COLORS:
            donut_colors.append(CATEGORY_COLORS[cat])
        else:
            donut_colors.append(FALLBACK_COLORS[fallback_idx % len(FALLBACK_COLORS)])
            fallback_idx += 1
    donut_legend = [
        {'label': k, 'value': v, 'color': c}
        for (k, v), c in zip(donut_cats.items(), donut_colors)
    ]

    # Outstanding invoices total
    total_outstanding = sum(m.get('outstanding_invoices', 0) for m in months_data)

    # YTD contractor totals (across all months)
    ytd_contractors = {}
    for m in months_data:
        for name, amt in m.get('contractors', {}).items():
            ytd_contractors[name] = round(ytd_contractors.get(name, 0) + amt, 2)

    # Per-client invoice totals (parse from uploaded CSVs)
    client_totals = {}
    client_last_invoice = {}
    for f in UPLOADS_DIR.iterdir():
        if 'invoice' in f.name.lower() and f.suffix.lower() == '.csv':
            try:
                for inv in parse_invoices(f):
                    c = inv['client']
                    if c:
                        client_totals[c] = round(client_totals.get(c, 0) + inv['amount'], 2)
                        if c not in client_last_invoice or inv['date'] > client_last_invoice[c]:
                            client_last_invoice[c] = inv['date']
            except:
                pass

    # Build clients dict — merge QB data with CLIENT_META and poto-data.json overrides
    saved_clients = poto_data.get('clients', {})
    clients = {}
    for name, total in client_totals.items():
        if saved_clients.get(name, {}).get('hidden'):
            continue
        meta  = CLIENT_META.get(name, {})
        saved = saved_clients.get(name, {})
        base  = {
            'type':            meta.get('type', 'One-Time'),
            'known_as':        meta.get('known_as', ''),
            'start':           meta.get('start', '') or '',
            'end':             meta.get('end', '') or '',
            'notes':           '',
            'ytd':             total,
            'last_invoice':    client_last_invoice.get(name, ''),
            'from_qb':         True,
        }
        # Apply saved overrides (skip financial read-only fields)
        financial_keys = {'ytd', 'last_invoice', 'from_qb'}
        base.update({k: v for k, v in saved.items() if k not in financial_keys})
        clients[name] = base

    # Manually added clients (not in QB)
    for name, saved_meta in saved_clients.items():
        if name not in clients and not saved_meta.get('hidden'):
            clients[name] = {'ytd': 0, 'last_invoice': '', 'from_qb': False, **saved_meta}

    # Build contractors dict
    saved_contractors = poto_data.get('contractors', {})
    contractors = {}
    for name, ytd in ytd_contractors.items():
        if saved_contractors.get(name, {}).get('hidden'):
            continue
        meta = CONTRACTOR_META.get(name, {})
        saved = saved_contractors.get(name, {})
        base = {
            'type':      meta.get('type', 'Per-Project'),
            'contracts': [{'client': c['client'], 'start': c['start'], 'end': c['end']}
                          for c in meta.get('contracts', [])],
            'ytd':       ytd,
            'from_qb':   True,
        }
        base.update({k: v for k, v in saved.items() if k not in {'ytd', 'from_qb'}})
        contractors[name] = base

    # Manually added contractors
    for name, saved_meta in saved_contractors.items():
        if name not in contractors and not saved_meta.get('hidden'):
            contractors[name] = {'ytd': 0, 'from_qb': False, **saved_meta}

    # Recent invoices list
    all_invoices = []
    for f in UPLOADS_DIR.iterdir():
        if 'invoice' in f.name.lower() and f.suffix.lower() == '.csv':
            m = re.search(r'([a-z]{3})[_-](\d{4})', f.name.lower())
            short = (f"{m.group(1).capitalize()} {m.group(2)[2:]}") if m else ''
            try:
                for inv in parse_invoices(f):
                    all_invoices.append({**inv, 'month_label': short})
            except:
                pass
    all_invoices.sort(key=lambda x: x['date'], reverse=True)

    # 6-month history strip
    hist_months = months_data[-6:]
    max_income  = max((m['income'] for m in hist_months), default=1) or 1
    history_strip = [{
        'short':       m['short'],
        'income':      m['income'],
        'net_income':  m.get('net_income', 0),
        'income_pct':  int(m['income'] / max_income * 100),
        'net_pct':     min(100, int(abs(m.get('net_income', 0)) / max_income * 100)),
        'net_positive': m.get('net_income', 0) >= 0,
    } for m in hist_months]

    return {
        'latest': latest,
        'latest_label': latest['label'],
        'stats': {
            'income':        latest['income'],
            'outstanding':   total_outstanding,
            'cogs':          latest.get('cogs', 0),
            'net_income':    latest.get('net_income', 0),
            'charlotte_draw': latest.get('charlotte_draw', 0),
            'eric_draw':     latest.get('eric_draw', 0),
        },
        'chart': {
            'labels':   chart_labels,
            'income':   chart_income,
            'expenses': chart_expenses,
            'net':      chart_net,
        },
        'donut': {
            'labels': list(donut_cats.keys()),
            'values': list(donut_cats.values()),
            'colors': donut_colors,
            'legend': donut_legend,
        },
        'clients':         clients,
        'contractors':     contractors,
        'ytd_contractors': ytd_contractors,
        'recent_invoices': all_invoices[:15],
        'history_strip':   history_strip,
        'loaded_months':   [{'key': k, 'label': history[k]['label']} for k in sorted_keys],
    }


# ── Rule-based insights fallback ─────────────────────────────────────────────────
def rule_based_insights(months_data):
    insights = []
    latest = months_data[-1]
    prev   = months_data[-2] if len(months_data) >= 2 else None

    if prev:
        delta = latest['income'] - prev['income']
        pct   = abs(delta / prev['income'] * 100) if prev['income'] else 0
        d     = "up" if delta > 0 else "down"
        insights.append(
            f"Income is <strong>{d} {pct:.0f}%</strong> vs. {prev.get('short', prev['label'])} "
            f"({fmt(prev['income'])} → {fmt(latest['income'])}). "
            + ("Strong month." if delta > 0 else "Worth watching.")
        )

    net = latest.get('net_income', 0)
    short = latest.get('short', latest['label'])
    if net > 0:
        gm = latest.get('gross_profit', 0) / latest['income'] * 100 if latest.get('income', 0) > 0 else 0
        insights.append(f"{short} closed with a <strong>net profit of {fmt(net)}</strong>. Gross margin: {gm:.0f}%.")
    else:
        insights.append(f"{short} ran a <strong>net loss of {fmt(abs(net))}</strong>. Review contractor costs vs. pipeline.")

    total_draws = latest.get('charlotte_draw', 0) + latest.get('eric_draw', 0)
    if total_draws > 0:
        insights.append(
            f"Owner draws: Charlotte {fmt(latest.get('charlotte_draw', 0))}, "
            f"Eric {fmt(latest.get('eric_draw', 0))} — total {fmt(total_draws)}."
        )

    if latest.get('expense_categories'):
        top_cat = max(latest['expense_categories'], key=latest['expense_categories'].get)
        insights.append(f"Largest expense: <strong>{top_cat}</strong> at {fmt(latest['expense_categories'][top_cat])}.")

    if latest.get('income', 0) > 0:
        ratio = latest.get('cogs', 0) / latest['income'] * 100
        insights.append(
            f"Contract labor was <strong>{ratio:.0f}% of revenue</strong> ({fmt(latest.get('cogs', 0))}). "
            + ("Within healthy range." if ratio < 60 else "High — worth reviewing margin.")
        )

    return insights[:6]


# ── Routes ───────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/dashboard')
def api_dashboard():
    try:
        data = build_dashboard_data()
        if data is None:
            return jsonify({'error': 'no_data'})
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload', methods=['POST'])
def api_upload():
    files = request.files.getlist('files')
    if not files or all(not f.filename for f in files):
        return jsonify({'success': False, 'message': 'No files provided.'}), 400

    saved = []
    for f in files:
        if f.filename:
            # Sanitize: keep only safe characters
            clean = re.sub(r'[^\w\-_. ]', '', f.filename.replace(' ', '_')).strip()
            if clean:
                f.save(str(UPLOADS_DIR / clean))
                saved.append(clean)

    if not saved:
        return jsonify({'success': False, 'message': 'No valid files to save.'}), 400

    # Process and update history
    try:
        months_data = process_uploads_dir()
        if not months_data:
            return jsonify({
                'success': False,
                'message': 'Files saved but could not find complete month data. '
                           'Make sure you have all 3 files (profit-loss, transactions, invoices) '
                           'for each month, named correctly.',
                'saved': saved,
            })

        history = load_history()
        for m in months_data:
            history[m['key']] = {
                'label':            m['label'],
                'short':            m['short'],
                'income':           m['income'],
                'cogs':             m['cogs'],
                'gross_profit':     m['gross_profit'],
                'total_expenses':   m['total_expenses'],
                'net_income':       m['net_income'],
                'charlotte_draw':   m['charlotte_draw'],
                'eric_draw':        m['eric_draw'],
                'contractors':      m['contractors'],
                'expense_categories': m['expense_categories'],
                'invoice_count':    len(m['invoices']),
                'outstanding_invoices': sum(i['open_balance'] for i in m['invoices']),
            }
        save_history(history)

        labels = [m['label'] for m in months_data]
        return jsonify({
            'success': True,
            'message': f"Processed {len(months_data)} month(s): {', '.join(labels)}",
            'saved': saved,
            'months': labels,
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error processing: {str(e)}', 'saved': saved})


@app.route('/api/clients', methods=['POST'])
def api_save_clients():
    poto_data = load_poto_data()
    poto_data['clients'] = request.get_json().get('clients', {})
    save_poto_data(poto_data)
    return jsonify({'success': True})


@app.route('/api/contractors', methods=['POST'])
def api_save_contractors():
    poto_data = load_poto_data()
    poto_data['contractors'] = request.get_json().get('contractors', {})
    save_poto_data(poto_data)
    return jsonify({'success': True})


@app.route('/api/insights')
def api_insights():
    history = load_history()
    if not history:
        return jsonify({'insights': [], 'source': 'none'})

    sorted_keys = sorted(history.keys())
    months_data = [history[k] for k in sorted_keys]

    # Ensure 'short' exists on each month
    for k, m in zip(sorted_keys, months_data):
        if 'short' not in m:
            parts = k.split('-')
            if len(parts) == 2:
                mo = int(parts[1])
                m['short'] = f"{MONTH_SHORT[mo]} {parts[0][2:]}"

    # Try Claude first; fall back to rule-based
    try:
        latest = months_data[-1]
        prev   = months_data[-2] if len(months_data) >= 2 else None

        prompt = (
            "You are analyzing finances for Poto Projects, a 2-person creative agency "
            "(Charlotte Lao + Eric Yang).\n\n"
            f"LATEST MONTH DATA:\n{json.dumps(latest, indent=2)}\n\n"
            + (f"PREVIOUS MONTH:\n{json.dumps(prev, indent=2)}\n\n" if prev else "")
            + "Generate exactly 5 concise financial insights. Use <strong> tags for key numbers. "
            "Focus on: income trend, net profit/loss, owner draws, top expense, contractor costs vs revenue. "
            'Return JSON: {"insights": ["html string", ...]}'
        )
        response = claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        # Extract JSON from response
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return jsonify({'insights': parsed.get('insights', []), 'source': 'claude'})
    except Exception as e:
        print(f"Claude insights error: {e}")

    # Fallback
    return jsonify({'insights': rule_based_insights(months_data), 'source': 'fallback'})


@app.route('/api/ask', methods=['POST'])
def api_ask():
    """Stream a Q&A response from Claude using Server-Sent Events."""
    data     = request.get_json()
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'No question.'}), 400

    history   = load_history()
    poto_data = load_poto_data()

    system_prompt = f"""You are the financial advisor for Poto Projects, a 2-person creative and distribution agency in the San Francisco Bay Area run by Charlotte Lao (CFO) and Eric Yang (CEO).

BUSINESS CONTEXT:
- Clients include Jeremy Lin (social + podcast), Bobby Portis (NBA), Kindred Ventures, and brands
- They work on US platforms (Instagram, TikTok, YouTube) and Chinese platforms (Weibo, Douyin, RedNote)
- Charlotte also freelances under @claophoto (luxury hotels and lifestyle brands)

FINANCIAL HISTORY (all months):
{json.dumps(history, indent=2)}

SAVED CLIENT METADATA:
{json.dumps(poto_data.get('clients', {}), indent=2)}

CHARLOTTE'S ESTABLISHED RATES:
- Full day shoot: ~$1,000
- Half day (4 hours): ~$550
- Commercial day rate (hotel/brand + usage): $1,500–$2,000+
- Complex campaigns with paid ads usage: $6,500+ (Four Seasons model)

Answer questions directly using the actual data above. Be specific with numbers. Today: {date.today().strftime('%B %d, %Y')}."""

    def generate():
        try:
            with claude.messages.stream(
                model="claude-opus-4-6",
                max_tokens=2048,
                thinking={"type": "adaptive"},
                system=system_prompt,
                messages=[{"role": "user", "content": question}]
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/quote', methods=['POST'])
def api_quote():
    """Generate a project quote using Claude."""
    data = request.get_json()

    prompt = f"""Generate a detailed quote for a Poto Projects / Charlotte Lao (@claophoto) project.

PROJECT DETAILS:
- Project type: {data.get('project_type', 'Not specified')}
- Client type: {data.get('client_type', 'Not specified')}
- Deliverables: {data.get('deliverables', 'Not specified')}
- Usage rights: {data.get('usage_rights', 'Not specified')}
- Timeline: {data.get('timeline', 'Not specified')}
- Additional notes: {data.get('notes', '')}

CHARLOTTE'S ESTABLISHED RATE REFERENCE:
- Full production day: ~$1,000
- Half day (4 hours): ~$550
- 2-hour minimum: ~$375–$400
- Commercial day rate (hotel/brand + usage): $1,500–$2,000+
- Complex campaigns with model, multi-platform paid ads: $6,500+ (Four Seasons benchmark)

PRICING RULES (always apply):
1. Usage rights are additive: paid social ads = premium, organic = standard, exclusivity = major premium
2. Factor contractor costs (second shooter, editor, drone op) with margin
3. Never quote without scope — assume the details above define it
4. Break down into line items so the client sees the value

PROVIDE:
1. Recommended total (with low/high confidence range)
2. Line-by-line breakdown
3. Pricing rationale (why this number)
4. What could shift the price up or down
5. One short paragraph Charlotte can paste into a client email"""

    try:
        response = claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return jsonify({'quote': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(debug=True, port=5000)
