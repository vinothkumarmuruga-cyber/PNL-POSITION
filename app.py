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
# Build the display table — two rows (CE, PE) per position, same
# column layout as before with TGT added right before exit.
# ------------------------------------------------------------
rows = []
for p in positions:
    lot = p.get('lot_size') or 0
    for leg in ('ce', 'pe'):
        entry = float(p.get(f'{leg}_entry') or 0)
        exit_ = p.get(f'{leg}_exit')
        exit_ = float(exit_) if exit_ not in (None, '') else None
        qty = int(p.get(f'{leg}_qty') or 0)
        ltp = leg_ltp(p.get(f'{leg}_instrument_key'))
        is_open = exit_ is None
        effective_exit = ltp if is_open else exit_
        points = (effective_exit - entry) * qty
        invest = entry * lot * qty
        profit = points * lot
        rows.append({
            'S.no': p['sno'],
            'Entry Date': p.get('entry_date'),
            'SYMBOL': p['symbol'],
            'STRIKE': f"{p[f'{leg}_strike']:.0f} {leg.upper()}",
            'lot Size': lot,
            'Qty': qty,
            'entry': entry,
            'LTP': ltp,
            'TGT': float(p.get(f'{leg}_tgt') or 0),
            'exit': exit_,
            'points': points,
            'invest': invest,
            'profit': profit,
            'Exit Date': p.get('exit_date'),
            'remarks': p.get('remarks') or '',
            '_is_open': is_open,
        })

df = pd.DataFrame(rows)
totals = df.groupby('S.no')[['invest', 'profit']].transform('sum')
df['Net Invest'] = totals['invest']
df['Net Profit'] = totals['profit']
df['profit%'] = (df['Net Profit'] / df['Net Invest'].replace(0, pd.NA) * 100).fillna(0.0)

display_cols = [
    'S.no', 'Entry Date', 'SYMBOL', 'STRIKE', 'lot Size', 'Qty',
    'entry', 'LTP', 'TGT', 'exit', 'points', 'invest', 'profit',
    'Net Invest', 'Net Profit', 'profit%', 'Exit Date', 'remarks'
]
show_df = df[display_cols].copy()
for dc in ('Entry Date', 'Exit Date'):
    show_df[dc] = pd.to_datetime(show_df[dc], errors='coerce').dt.strftime('%d-%m-%Y')

open_legs = int(df['_is_open'].sum())
total_invest = df.drop_duplicates('S.no')['Net Invest'].sum()
total_profit = df.drop_duplicates('S.no')['Net Profit'].sum()
overall_pct = (total_profit / total_invest * 100) if total_invest else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Open Legs", open_legs)
m2.metric("Total Invested", f"₹{total_invest:,.0f}")
m3.metric("Total Net Profit", f"₹{total_profit:,.0f}")
m4.metric("Overall %", f"{overall_pct:.1f}%")


def color_pnl(val):
    if not isinstance(val, (int, float)):
        return ''
    if val > 0:
        return 'background-color: #d4edda; color: #155724; font-weight: 700'
    if val < 0:
        return 'background-color: #f8d7da; color: #721c24; font-weight: 700'
    return ''


def color_tgt_hit(row):
    # Highlight LTP once it has reached that leg's TGT.
    styles = [''] * len(row)
    try:
        ltp_idx = show_df.columns.get_loc('LTP')
        if row['TGT'] > 0 and row['LTP'] >= row['TGT']:
            styles[ltp_idx] = 'background-color: darkgreen; color: white; font-weight: 700'
    except Exception:
        pass
    return styles


format_dict = {
    'entry': '{:.2f}', 'LTP': '{:.2f}', 'TGT': '{:.2f}', 'exit': '{:.2f}', 'points': '{:.2f}',
    'invest': '{:,.0f}', 'profit': '{:,.0f}', 'Net Invest': '{:,.0f}',
    'Net Profit': '{:,.0f}', 'profit%': '{:.1f}%'
}

styled = (
    show_df.style
    .apply(color_tgt_hit, axis=1)
    .map(color_pnl, subset=['points', 'profit', 'Net Profit', 'profit%'])
    .set_properties(subset=['SYMBOL'], **{'background-color': '#dbeeff'})
    .format(format_dict, na_rep='—')
    .set_properties(**{'text-align': 'center', 'font-size': '15px'})
)

st.caption(f"Last Updated: {get_ist_now().strftime('%H:%M:%S')} IST")
st.dataframe(styled, hide_index=True, width='stretch', height=min(600, 60 + 40 * len(show_df)))

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
