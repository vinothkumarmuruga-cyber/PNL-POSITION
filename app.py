import streamlit as st
import pandas as pd
import requests
import os
import re
import json
import time
import gzip
import shutil
import concurrent.futures
import html as html_lib
from datetime import datetime, timedelta, timezone

# ============================================================
# IST helpers
# ============================================================
IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def get_ist_now():
    return datetime.now(IST)


st.set_page_config(page_title="Hedge PNL Tracker", layout="wide")

st.markdown("""
    <style>
        .block-container { padding-top: 1rem !important; padding-bottom: 1rem !important; }
        h1 { font-size: 1.8rem !important; margin-bottom: 0.3rem !important; }
        div[data-testid="stDataFrame"] { font-weight: 600 !important; }
    </style>
""", unsafe_allow_html=True)

# ============================================================
# SIMPLE BY DESIGN
#
# Every position here is exactly one hedge: one symbol, a CE leg and a PE
# leg. No spreadsheet-style editable grid — that's what kept breaking
# (Streamlit's st.data_editor has real, reproducible bugs around dynamic
# rows and date/number columns). Instead: fill a form to open a position,
# fill a small form to close it. Plain widgets, no fragile grid.
# ============================================================
DATA_DIR = 'data'
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

TOKEN_FILE = os.path.join(DATA_DIR, 'token.json')
LTP_CACHE_FILE = os.path.join(DATA_DIR, 'ltp_cache.json')
POSITIONS_FILE = os.path.join(DATA_DIR, 'hedge_positions.json')
NSE_JSON_PATH = 'NSE.json'


# ============================================================
# Token
# ============================================================
def load_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
                data = json.load(f)
                if data.get('date') == get_ist_now().strftime('%Y-%m-%d'):
                    return data.get('token', '')
        except Exception:
            pass
    return ''


def save_token(token):
    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump({'date': get_ist_now().strftime('%Y-%m-%d'), 'token': token}, f)
    except Exception:
        pass


# ============================================================
# LTP cache + fetch
# ============================================================
def load_ltp_cache():
    if os.path.exists(LTP_CACHE_FILE):
        try:
            with open(LTP_CACHE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_ltp_cache(new_data):
    try:
        cache = load_ltp_cache()
        cache.update(new_data)
        with open(LTP_CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except Exception:
        pass


def fetch_ltp(instrument_keys, token):
    if not token or not instrument_keys:
        return {}
    url = "https://api.upstox.com/v3/market-quote/ltp"
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    batch_size = 50
    ltp_map = {}
    batches = [instrument_keys[i:i + batch_size] for i in range(0, len(instrument_keys), batch_size)]

    def fetch_batch(batch):
        params = {'instrument_key': ','.join(batch)}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    result = {}
                    for _, details in data.get('data', {}).items():
                        inst_token = details.get('instrument_token')
                        last_price = details.get('last_price')
                        if inst_token is not None:
                            result[inst_token] = last_price
                    return result
        except Exception:
            pass
        return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_batch, b) for b in batches]
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
                if res:
                    ltp_map.update(res)
            except Exception:
                pass
    return ltp_map


# ============================================================
# NSE instrument master — only needed when OPENING a new position
# (to resolve instrument_key + lot_size for the current expiry).
# ============================================================
@st.cache_data
def load_nse_json():
    if not os.path.exists(NSE_JSON_PATH):
        return pd.DataFrame()
    try:
        df = pd.read_json(NSE_JSON_PATH)
        if 'segment' in df.columns:
            df = df[df['segment'] == 'NSE_FO']
        df['expiry_dt'] = pd.to_datetime(df['expiry'], unit='ms').dt.normalize()
        df['strike_price'] = df['strike_price'].astype(float).round(2)
        return df
    except Exception as e:
        st.error(f"Error loading NSE.json: {e}")
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=300)
def resolve_current_contract(symbol, strike, option_type, today_str):
    """
    Current (nearest unexpired) contract for symbol/strike/type.
    Returns (instrument_key, lot_size, expiry_str) or (None, None, None).
    Resolved ONCE when a position is opened and then stored with it — a
    live position's contract doesn't need to keep re-resolving itself.
    """
    df = load_nse_json()
    if df.empty or not symbol or strike is None or not option_type:
        return None, None, None
    today = pd.to_datetime(today_str).normalize()
    match = df[
        (df['underlying_symbol'].astype(str).str.upper() == str(symbol).upper()) &
        (df['strike_price'] == round(float(strike), 2)) &
        (df['instrument_type'].astype(str).str.upper() == option_type.upper()) &
        (df['expiry_dt'] >= today)
    ]
    if match.empty:
        return None, None, None
    row = match.sort_values('expiry_dt').iloc[0]
    inst_key = row.get('instrument_key')
    lot_size = row.get('lot_size')
    lot_size = int(lot_size) if pd.notna(lot_size) else None
    expiry_dt = row.get('expiry_dt')
    expiry_str = expiry_dt.strftime('%Y-%m-%d') if pd.notna(expiry_dt) else None
    return inst_key, lot_size, expiry_str


# ============================================================
# Position storage — a plain list of dicts, one per hedge (CE+PE legs
# baked in together). No dataframe editing involved anywhere, so none of
# the data_editor dtype/row fragility applies here.
# ============================================================
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_positions(positions):
    try:
        with open(POSITIONS_FILE, 'w') as f:
            json.dump(positions, f, indent=2, default=str)
    except Exception as e:
        st.error(f"Could not save positions: {e}")


def next_sno(positions):
    if not positions:
        return 1
    return max(p.get('sno', 0) for p in positions) + 1


# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.header("Configuration")
    saved_token = load_token()
    access_token = st.text_input("Upstox Access Token", value=saved_token, type="password")
    if access_token and access_token != saved_token:
        save_token(access_token)

    st.markdown("---")
    st.subheader("NSE Instrument JSON")
    st.caption(f"{'✅ Found' if os.path.exists(NSE_JSON_PATH) else '❌ Missing'}: {NSE_JSON_PATH} (needed only to open new positions)")
    if st.button("🔄 Download Latest NSE.json", use_container_width=True):
        try:
            with st.spinner("Downloading NSE.json from Upstox..."):
                url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
                headers = {"User-Agent": "Mozilla/5.0"}
                response = requests.get(url, headers=headers, stream=True)
                if response.status_code == 200:
                    with open(NSE_JSON_PATH, "wb") as f_out:
                        with gzip.GzipFile(fileobj=response.raw) as f_in:
                            shutil.copyfileobj(f_in, f_out)
                    st.cache_data.clear()
                    st.success("Updated.")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Download failed: HTTP {response.status_code}")
        except Exception as e:
            st.error(f"Error: {e}")

    st.markdown("---")
    st.header("Auto Refresh")
    auto_refresh = st.checkbox("Enable Auto-Refresh", value=False)
    refresh_interval = st.slider("Refresh Interval (seconds)", min_value=5, max_value=60, value=15)

# ============================================================
# Main page
# ============================================================
st.title("Hedge PNL Tracker")
st.caption("Every position = one symbol, one CE leg, one PE leg. LTP is the only thing pulled from the API automatically.")

positions = load_positions()

# ------------------------------------------------------------
# Open a new position
# ------------------------------------------------------------
with st.expander("➕ Add Position", expanded=(len(positions) == 0)):
    with st.form("add_position_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        entry_date = c1.date_input("Entry Date", value=get_ist_now().date())
        symbol = c2.text_input("Symbol", placeholder="e.g. KOTAKBANK").strip().upper()

        st.markdown("**CE leg**")
        ce1, ce2, ce3, ce4 = st.columns(4)
        ce_strike = ce1.number_input("CE Strike", min_value=0.0, step=0.5, format="%.1f", key="ce_strike")
        ce_entry = ce2.number_input("CE Entry", min_value=0.0, step=0.05, format="%.2f", key="ce_entry")
        ce_tgt = ce3.number_input("CE TGT", min_value=0.0, step=0.05, format="%.2f", key="ce_tgt")
        ce_qty = ce4.number_input("CE Qty", min_value=1, step=1, value=1, key="ce_qty")

        st.markdown("**PE leg**")
        pe1, pe2, pe3, pe4 = st.columns(4)
        pe_strike = pe1.number_input("PE Strike", min_value=0.0, step=0.5, format="%.1f", key="pe_strike")
        pe_entry = pe2.number_input("PE Entry", min_value=0.0, step=0.05, format="%.2f", key="pe_entry")
        pe_tgt = pe3.number_input("PE TGT", min_value=0.0, step=0.05, format="%.2f", key="pe_tgt")
        pe_qty = pe4.number_input("PE Qty", min_value=1, step=1, value=1, key="pe_qty")

        remarks = st.text_input("Remarks", value="")

        submitted = st.form_submit_button("Add Position", use_container_width=True)

        if submitted:
            if not symbol or ce_strike <= 0 or pe_strike <= 0 or ce_entry <= 0 or pe_entry <= 0:
                st.error("Symbol, both strikes and both entry prices are required.")
            else:
                today_str = get_ist_now().strftime('%Y-%m-%d')
                ce_key, lot_size, expiry_str = resolve_current_contract(symbol, ce_strike, "CE", today_str)
                pe_key, pe_lot_size, _ = resolve_current_contract(symbol, pe_strike, "PE", today_str)
                lot_size = lot_size or pe_lot_size
                if not ce_key or not pe_key:
                    st.warning(
                        "Couldn't match one or both legs to a live contract in NSE.json "
                        "(download it in the sidebar first). Position added anyway — "
                        "LTP will show 0 until it resolves."
                    )
                new_pos = {
                    'sno': next_sno(positions),
                    'entry_date': str(entry_date),
                    'symbol': symbol,
                    'lot_size': lot_size or 0,
                    'expiry': expiry_str,
                    'ce_strike': ce_strike, 'ce_entry': ce_entry, 'ce_tgt': ce_tgt,
                    'ce_qty': int(ce_qty), 'ce_exit': None, 'ce_instrument_key': ce_key,
                    'pe_strike': pe_strike, 'pe_entry': pe_entry, 'pe_tgt': pe_tgt,
                    'pe_qty': int(pe_qty), 'pe_exit': None, 'pe_instrument_key': pe_key,
                    'exit_date': None,
                    'remarks': remarks,
                }
                positions.append(new_pos)
                save_positions(positions)
                st.success(f"Added S.no {new_pos['sno']} — {symbol}")
                st.rerun()

if not positions:
    st.info("No positions yet. Use **Add Position** above to open your first hedge.")
    st.stop()

# ------------------------------------------------------------
# Live LTP for every open leg
# ------------------------------------------------------------
all_keys = sorted({
    p[k] for p in positions for k in ('ce_instrument_key', 'pe_instrument_key')
    if p.get(k)
})
ltp_cache = load_ltp_cache()
if access_token and all_keys:
    ist_now = get_ist_now()
    is_market_hours = datetime.strptime("09:00", "%H:%M").time() <= ist_now.time() <= datetime.strptime("15:40", "%H:%M").time()
    missing_keys = [k for k in all_keys if k not in ltp_cache]
    keys_to_fetch = all_keys if is_market_hours else missing_keys
    if keys_to_fetch:
        fetched = fetch_ltp(keys_to_fetch, access_token)
        if fetched:
            save_ltp_cache(fetched)
            ltp_cache = load_ltp_cache()
elif not access_token:
    st.warning("Enter your Upstox Access Token in the sidebar to see live LTP.")


def leg_ltp(inst_key):
    return float(ltp_cache.get(inst_key, 0.0)) if inst_key else 0.0


# ------------------------------------------------------------
# Build the display table — spreadsheet-style: one row PER LEG (CE, PE)
# like the original Excel sheet, with the shared fields (S.no, Entry
# Date, Symbol, lot Size, Net Invest, Net Profit, profit%, Exit Date,
# Remarks) merged (rowspan) across the two leg rows instead of repeated.
# ------------------------------------------------------------
def esc(v):
    return html_lib.escape(str(v))


def pnl_style(val):
    if val > 0:
        return 'background-color:#d4edda;color:#155724;font-weight:700'
    if val < 0:
        return 'background-color:#f8d7da;color:#721c24;font-weight:700'
    return ''


open_legs = 0
total_invest = 0.0
total_profit = 0.0
body_rows_html = []

for pos_idx, p in enumerate(positions):
    lot = p.get('lot_size') or 0
    leg_calc = {}
    for leg in ('ce', 'pe'):
        entry = float(p.get(f'{leg}_entry') or 0)
        exit_ = p.get(f'{leg}_exit')
        exit_ = float(exit_) if exit_ not in (None, '') else None
        qty = int(p.get(f'{leg}_qty') or 0)
        ltp = leg_ltp(p.get(f'{leg}_instrument_key'))
        tgt = float(p.get(f'{leg}_tgt') or 0)
        is_open = exit_ is None
        effective_exit = ltp if is_open else exit_
        points = (effective_exit - entry) * qty
        invest = entry * lot * qty
        profit = points * lot
        leg_calc[leg] = {
            'strike': p[f'{leg}_strike'], 'qty': qty, 'entry': entry, 'ltp': ltp,
            'tgt': tgt, 'exit': exit_, 'points': points, 'invest': invest,
            'profit': profit, 'is_open': is_open,
        }

    net_invest = leg_calc['ce']['invest'] + leg_calc['pe']['invest']
    net_profit = leg_calc['ce']['profit'] + leg_calc['pe']['profit']
    if net_profit == 0:
        net_profit = 0.0  # avoid displaying "-0"
    net_pct = (net_profit / net_invest * 100) if net_invest else 0.0

    open_legs += int(leg_calc['ce']['is_open']) + int(leg_calc['pe']['is_open'])
    total_invest += net_invest
    total_profit += net_profit

    entry_date_str = pd.to_datetime(p.get('entry_date'), errors='coerce')
    entry_date_str = entry_date_str.strftime('%d-%m-%Y') if pd.notna(entry_date_str) else '—'
    exit_date_str = pd.to_datetime(p.get('exit_date'), errors='coerce')
    exit_date_str = exit_date_str.strftime('%d-%m-%Y') if pd.notna(exit_date_str) else '—'

    band = 'row-band-b' if pos_idx % 2 else 'row-band-a'

    for i, leg in enumerate(('ce', 'pe')):
        lc = leg_calc[leg]
        tgt_hit = lc['tgt'] > 0 and lc['ltp'] >= lc['tgt']
        ltp_style = 'background-color:#0b6623;color:#fff;font-weight:700' if tgt_hit else ''
        exit_disp = f"{lc['exit']:.2f}" if lc['exit'] is not None else '—'

        cells = []
        if i == 0:
            cells.append(f'<td rowspan="2" class="{band}">{p["sno"]}</td>')
            cells.append(f'<td rowspan="2" class="{band}">{entry_date_str}</td>')
            cells.append(f'<td rowspan="2" class="{band} sym-cell">{esc(p["symbol"])}</td>')
            cells.append(f'<td rowspan="2" class="{band}">{lot}</td>')
        cells.append(f'<td class="{band}">{lc["strike"]:.0f} {leg.upper()}</td>')
        cells.append(f'<td class="{band}">{lc["qty"]}</td>')
        cells.append(f'<td class="{band} entry-cell">{lc["entry"]:.2f}</td>')
        cells.append(f'<td class="{band}" style="{ltp_style}">{lc["ltp"]:.2f}</td>')
        cells.append(f'<td class="{band}">{lc["tgt"]:.2f}</td>')
        cells.append(f'<td class="{band} exit-cell">{exit_disp}</td>')
        cells.append(f'<td class="{band}" style="{pnl_style(lc["points"])}">{lc["points"]:.2f}</td>')
        cells.append(f'<td class="{band}">{lc["invest"]:,.0f}</td>')
        cells.append(f'<td class="{band}" style="{pnl_style(lc["profit"])}">{lc["profit"]:,.0f}</td>')
        if i == 0:
            cells.append(f'<td rowspan="2" class="{band}" style="{pnl_style(net_profit)}">{net_invest:,.0f}</td>')
            cells.append(f'<td rowspan="2" class="{band}" style="{pnl_style(net_profit)}">{net_profit:,.0f}</td>')
            cells.append(f'<td rowspan="2" class="{band}" style="{pnl_style(net_profit)}">{net_pct:.1f}%</td>')
            cells.append(f'<td rowspan="2" class="{band}">{exit_date_str}</td>')
            cells.append(f'<td rowspan="2" class="{band}">{esc(p.get("remarks") or "")}</td>')
        body_rows_html.append('<tr>' + ''.join(cells) + '</tr>')

overall_pct = (total_profit / total_invest * 100) if total_invest else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Open Legs", open_legs)
m2.metric("Total Invested", f"₹{total_invest:,.0f}")
m3.metric("Total Net Profit", f"₹{total_profit:,.0f}")
m4.metric("Overall PNL %", f"{overall_pct:.1f}%")

st.markdown("""
    <style>
        .pnl-table-wrap { overflow-x: auto; border: 1px solid #d0d0d0; border-radius: 6px; }
        table.pnl-table { border-collapse: collapse; width: 100%; font-size: 14px; white-space: nowrap; }
        table.pnl-table th, table.pnl-table td {
            border: 1px solid #d0d0d0; padding: 6px 10px; text-align: center;
        }
        table.pnl-table thead th {
            background-color: #f4a261; color: #1a1a1a; font-weight: 700;
            position: sticky; top: 0; z-index: 1;
        }
        table.pnl-table .row-band-a { background-color: #ffffff; }
        table.pnl-table .row-band-b { background-color: #f7f9fb; }
        table.pnl-table .sym-cell { background-color: #dbeeff !important; font-weight: 700; color: #0b3d91; }
        table.pnl-table .entry-cell { background-color: #c6efce; font-weight: 600; }
        table.pnl-table .exit-cell { background-color: #ffeb9c; font-weight: 600; }
    </style>
""", unsafe_allow_html=True)

table_html = f"""
<div class="pnl-table-wrap">
<table class="pnl-table">
<thead>
<tr>
    <th>S.no</th><th>Entry Date</th><th>SYMBOL</th><th>lot Size</th>
    <th>Strike</th><th>Qty</th><th>entry</th><th>LTP</th><th>TGT</th><th>exit</th>
    <th>points</th><th>invest</th><th>profit</th>
    <th>Net Invest</th><th>Net Profit</th><th>profit%</th><th>Exit Date</th><th>remarks</th>
</tr>
</thead>
<tbody>
{''.join(body_rows_html)}
</tbody>
</table>
</div>
"""

st.caption(f"Last Updated: {get_ist_now().strftime('%H:%M:%S')} IST")
st.markdown(table_html, unsafe_allow_html=True)

# ------------------------------------------------------------
# Close / edit a position — plain widgets, no grid editing.
# ------------------------------------------------------------
st.markdown("---")
st.subheader("Close / Edit a Position")

options = {f"S.no {p['sno']} — {p['symbol']}": p['sno'] for p in positions}
choice = st.selectbox("Position", options=list(options.keys()))
sel_sno = options[choice]
pos = next(p for p in positions if p['sno'] == sel_sno)

with st.form("edit_position_form"):
    st.caption("Entry / Qty")
    e1, e2, e3, e4 = st.columns(4)
    ce_entry_val = e1.number_input(
        "CE Entry", min_value=0.0, step=0.05, format="%.2f",
        value=float(pos.get('ce_entry') or 0.0)
    )
    ce_qty_val = e2.number_input(
        "CE Qty", min_value=1, step=1,
        value=int(pos.get('ce_qty') or 1)
    )
    pe_entry_val = e3.number_input(
        "PE Entry", min_value=0.0, step=0.05, format="%.2f",
        value=float(pos.get('pe_entry') or 0.0)
    )
    pe_qty_val = e4.number_input(
        "PE Qty", min_value=1, step=1,
        value=int(pos.get('pe_qty') or 1)
    )

    st.caption("Exit")
    c1, c2 = st.columns(2)
    ce_exit_val = c1.number_input(
        "CE Exit", min_value=0.0, step=0.05, format="%.2f",
        value=float(pos.get('ce_exit') or 0.0)
    )
    pe_exit_val = c2.number_input(
        "PE Exit", min_value=0.0, step=0.05, format="%.2f",
        value=float(pos.get('pe_exit') or 0.0)
    )
    exit_date_val = st.date_input(
        "Exit Date",
        value=pd.to_datetime(pos['exit_date']).date() if pos.get('exit_date') else get_ist_now().date()
    )
    remarks_val = st.text_input("Remarks", value=pos.get('remarks') or '')

    save_col, delete_col = st.columns(2)
    save_clicked = save_col.form_submit_button("💾 Save", use_container_width=True)
    delete_clicked = delete_col.form_submit_button("🗑️ Delete Position", use_container_width=True)

    if save_clicked:
        pos['ce_entry'] = ce_entry_val
        pos['pe_entry'] = pe_entry_val
        pos['ce_qty'] = int(ce_qty_val)
        pos['pe_qty'] = int(pe_qty_val)
        pos['ce_exit'] = ce_exit_val if ce_exit_val > 0 else None
        pos['pe_exit'] = pe_exit_val if pe_exit_val > 0 else None
        pos['exit_date'] = str(exit_date_val) if (ce_exit_val > 0 or pe_exit_val > 0) else None
        pos['remarks'] = remarks_val
        save_positions(positions)
        st.success(f"Saved S.no {sel_sno}")
        st.rerun()

    if delete_clicked:
        positions = [p for p in positions if p['sno'] != sel_sno]
        save_positions(positions)
        st.success(f"Deleted S.no {sel_sno}")
        st.rerun()

if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
