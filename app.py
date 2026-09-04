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
# IST helpers (same convention as the OTM Positional Scanner)
# ============================================================
IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def get_ist_now():
    return datetime.now(IST)


st.set_page_config(page_title="Positional PNL Tracker", layout="wide")

st.markdown("""
    <style>
        .block-container { padding-top: 1rem !important; padding-bottom: 1rem !important; }
        h1 { font-size: 1.8rem !important; margin-bottom: 0.3rem !important; }
        div[data-testid="stDataFrame"] { font-weight: 600 !important; }
    </style>
""", unsafe_allow_html=True)

# ============================================================
# Persistence — same data/ folder + NSE.json the scanner already uses.
# Run this file from the same folder as the OTM Positional Scanner
# (e.g. as a second page: pages/2_PNL_Tracker.py) so it can reuse the
# already-downloaded NSE.json and the saved Upstox token.
# ============================================================
DATA_DIR = 'data'
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

TOKEN_FILE = os.path.join(DATA_DIR, 'token.json')
LTP_CACHE_FILE = os.path.join(DATA_DIR, 'ltp_cache.json')
POSITIONS_FILE = os.path.join(DATA_DIR, 'pnl_positions.json')
NSE_JSON_PATH = 'NSE.json'

# Editable input columns — this is ALL the user ever types in.
INPUT_COLUMNS = [
    'S.no', 'Entry Date', 'STRATEGY', 'SYMBOL', 'STRIKE',
    'Expiry', 'Qty', 'entry', 'exit', 'Exit Date', 'remarks'
]
INPUT_DTYPES = {
    'S.no': 'Int64', 'Entry Date': 'object', 'STRATEGY': 'object',
    'SYMBOL': 'object', 'STRIKE': 'object', 'Expiry': 'object',
    'Qty': 'Int64', 'entry': 'float64', 'exit': 'float64',
    'Exit Date': 'object', 'remarks': 'object'
}


# ============================================================
# Token (shared with the scanner — reuse if already saved today)
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
# LTP cache (shared file with the scanner — same instrument_token keys)
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
    """Batch LTP fetch via Upstox v3 — identical logic to the scanner."""
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
# NSE instrument master — same file/shape the scanner downloads.
# Used ONLY to auto-resolve instrument_key + lot_size for a leg.
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


STRIKE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(CE|PE)', re.IGNORECASE)


def parse_strike(strike_text):
    """'420 CE' / '420CE' -> (420.0, 'CE'). Returns (None, None) if unparsable."""
    if not strike_text or not isinstance(strike_text, str):
        return None, None
    m = STRIKE_RE.search(strike_text.strip())
    if not m:
        return None, None
    return round(float(m.group(1)), 2), m.group(2).upper()


@st.cache_data(show_spinner=False)
def resolve_instrument(symbol, strike, option_type, expiry_str, _nse_json_mtime):
    """
    Looks up (instrument_key, lot_size) for one leg from NSE.json.
    _nse_json_mtime is only there to bust the cache when NSE.json is
    re-downloaded — it isn't used in the lookup itself.
    """
    df = load_nse_json()
    if df.empty or symbol is None or strike is None or option_type is None or not expiry_str:
        return None, None
    try:
        expiry_dt = pd.to_datetime(expiry_str).normalize()
    except Exception:
        return None, None
    match = df[
        (df['underlying_symbol'].astype(str).str.upper() == str(symbol).upper()) &
        (df['strike_price'] == round(float(strike), 2)) &
        (df['instrument_type'].astype(str).str.upper() == option_type.upper()) &
        (df['expiry_dt'] == expiry_dt)
    ]
    if match.empty:
        return None, None
    row = match.iloc[0]
    inst_key = row.get('instrument_key')
    lot_size = row.get('lot_size')
    lot_size = int(lot_size) if pd.notna(lot_size) else None
    return inst_key, lot_size


# ============================================================
# Position storage
# ============================================================
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, 'r') as f:
                data = json.load(f)
            df = pd.DataFrame(data)
            for c in INPUT_COLUMNS:
                if c not in df.columns:
                    df[c] = None
            return df[INPUT_COLUMNS]
        except Exception:
            pass
    return pd.DataFrame(columns=INPUT_COLUMNS)


def save_positions(df):
    try:
        clean = df.copy()
        # Blank strings -> None so JSON/date widgets round-trip cleanly
        clean = clean.where(pd.notna(clean), None)
        with open(POSITIONS_FILE, 'w') as f:
            json.dump(clean.to_dict(orient='records'), f, default=str)
    except Exception as e:
        st.error(f"Could not save positions: {e}")


# ============================================================
# Sidebar — token, NSE.json refresh, auto-refresh
# ============================================================
with st.sidebar:
    st.header("Configuration")
    saved_token = load_token()
    access_token = st.text_input("Upstox Access Token", value=saved_token, type="password")
    if access_token and access_token != saved_token:
        save_token(access_token)

    st.caption(
        "Only **LTP** and **lot Size** are filled in automatically "
        "(LTP live from Upstox, lot Size from NSE.json). Everything "
        "else — dates, strategy, symbol, strike, qty, entry, exit, "
        "remarks — you type in the table."
    )

    st.markdown("---")
    st.subheader("NSE Instrument JSON")
    st.caption(f"{'✅ Found' if os.path.exists(NSE_JSON_PATH) else '❌ Missing'}: {NSE_JSON_PATH}")
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
st.title("Positional PNL Tracker")
st.caption(
    "Give the same **S.no** to every leg of one strategy (e.g. both the "
    "CE and PE row of a spread) — Net Invest / Net Profit / profit% are "
    "totalled across all legs that share an S.no."
)

if 'positions_df' not in st.session_state:
    st.session_state.positions_df = load_positions()

nse_json_mtime = os.path.getmtime(NSE_JSON_PATH) if os.path.exists(NSE_JSON_PATH) else 0

# DateColumn requires an actual datetime dtype, not plain strings — the
# JSON store keeps dates as ISO strings, so convert on the way into the editor.
editor_input = st.session_state.positions_df.copy()
for _dc in ('Entry Date', 'Expiry', 'Exit Date'):
    editor_input[_dc] = pd.to_datetime(editor_input[_dc], errors='coerce')

edited_df = st.data_editor(
    editor_input,
    num_rows="dynamic",
    width='stretch',
    hide_index=True,
    key="positions_editor",
    column_config={
        'S.no': st.column_config.NumberColumn('S.no', help="Same number for every leg of one strategy", step=1),
        'Entry Date': st.column_config.DateColumn('Entry Date', format="DD-MM-YYYY"),
        'STRATEGY': st.column_config.TextColumn('STRATEGY', help="e.g. 2.1 BULL"),
        'SYMBOL': st.column_config.TextColumn('SYMBOL', help="e.g. KOTAKBANK"),
        'STRIKE': st.column_config.TextColumn('STRIKE', help="e.g. 420 CE"),
        'Expiry': st.column_config.DateColumn('Expiry', format="DD-MM-YYYY", help="Option expiry — needed to fetch LTP/lot size"),
        'Qty': st.column_config.NumberColumn('Qty', step=1),
        'entry': st.column_config.NumberColumn('entry', format="%.2f"),
        'exit': st.column_config.NumberColumn('exit', format="%.2f", help="Leave blank while the position is open"),
        'Exit Date': st.column_config.DateColumn('Exit Date', format="DD-MM-YYYY"),
        'remarks': st.column_config.TextColumn('remarks'),
    }
)

# Normalize the edited datetime columns back to plain 'YYYY-MM-DD' strings
# for storage/comparison/downstream lookups.
for _dc in ('Entry Date', 'Expiry', 'Exit Date'):
    edited_df[_dc] = pd.to_datetime(edited_df[_dc], errors='coerce').dt.strftime('%Y-%m-%d')
    edited_df[_dc] = edited_df[_dc].where(edited_df[_dc].notna(), None)

if not edited_df.equals(st.session_state.positions_df):
    st.session_state.positions_df = edited_df
    save_positions(edited_df)

if edited_df.dropna(how='all').empty:
    st.info("Add a row above for each leg of a position (S.no, dates, strategy, symbol, strike e.g. '420 CE', expiry, qty, entry). LTP and lot Size fill in automatically once a token and NSE.json are available.")
    st.stop()

# ============================================================
# Compute: resolve instrument -> lot size + LTP -> points/invest/profit
# ============================================================
work = edited_df.dropna(subset=['SYMBOL', 'STRIKE']).copy()

strike_parsed = work['STRIKE'].apply(parse_strike)
work['_strike_price'] = strike_parsed.apply(lambda t: t[0])
work['_option_type'] = strike_parsed.apply(lambda t: t[1])

resolved = work.apply(
    lambda r: resolve_instrument(r['SYMBOL'], r['_strike_price'], r['_option_type'], r['Expiry'], nse_json_mtime),
    axis=1
)
work['instrument_key'] = resolved.apply(lambda t: t[0])
work['lot Size'] = resolved.apply(lambda t: t[1])

unresolved = work[work['instrument_key'].isna() & work['SYMBOL'].notna() & work['_strike_price'].notna()]
if not unresolved.empty:
    st.warning(
        f"{len(unresolved)} row(s) couldn't be matched in NSE.json (check SYMBOL / STRIKE like '420 CE' / Expiry). "
        "LTP and lot Size will show 0 for those rows until they resolve."
    )

# --- Live LTP ---
all_keys = work['instrument_key'].dropna().unique().tolist()
if access_token and all_keys:
    ist_now = get_ist_now()
    is_market_hours = datetime.strptime("09:00", "%H:%M").time() <= ist_now.time() <= datetime.strptime("15:40", "%H:%M").time()
    ltp_cache = load_ltp_cache()
    missing_keys = [k for k in all_keys if k not in ltp_cache]
    keys_to_fetch = all_keys if is_market_hours else missing_keys
    if keys_to_fetch:
        fetched = fetch_ltp(keys_to_fetch, access_token)
        if fetched:
            save_ltp_cache(fetched)
            ltp_cache = load_ltp_cache()
    work['LTP'] = work['instrument_key'].map(lambda k: ltp_cache.get(k, 0.0) if pd.notna(k) else 0.0)
else:
    work['LTP'] = 0.0
    if not access_token:
        st.warning("Enter your Upstox Access Token in the sidebar to fetch live LTP.")

work['lot Size'] = work['lot Size'].fillna(0).astype(int)
work['entry'] = pd.to_numeric(work['entry'], errors='coerce').fillna(0.0)
work['exit'] = pd.to_numeric(work['exit'], errors='coerce')
work['Qty'] = pd.to_numeric(work['Qty'], errors='coerce').fillna(0).astype(int)

# Open position -> mark-to-market against live LTP. Closed -> use actual exit.
work['_is_open'] = work['exit'].isna()
work['_effective_exit'] = work['exit'].where(~work['_is_open'], work['LTP'])

# points = (exit - entry) * Qty   |   invest = entry * lotSize * Qty   |   profit = points * lotSize
work['points'] = (work['_effective_exit'] - work['entry']) * work['Qty']
work['invest'] = work['entry'] * work['lot Size'] * work['Qty']
work['profit'] = work['points'] * work['lot Size']

# Position-level totals, grouped by S.no, broadcast back onto every leg
totals = work.groupby('S.no')[['invest', 'profit']].transform('sum')
work['Net Invest'] = totals['invest']
work['Net Profit'] = totals['profit']
work['profit%'] = (work['Net Profit'] / work['Net Invest'].replace(0, pd.NA) * 100).fillna(0.0)

# ============================================================
# Display — same column order as the reference sheet, LTP after entry
# ============================================================
display_cols = [
    'S.no', 'Entry Date', 'STRATEGY', 'SYMBOL', 'STRIKE', 'lot Size', 'Qty',
    'entry', 'LTP', 'exit', 'points', 'invest', 'profit',
    'Net Invest', 'Net Profit', 'profit%', 'Exit Date', 'remarks'
]
show_df = work[display_cols].copy()

open_pos = int(work['_is_open'].sum())
total_invest = work.drop_duplicates('S.no')['Net Invest'].sum()
total_profit = work.drop_duplicates('S.no')['Net Profit'].sum()
overall_pct = (total_profit / total_invest * 100) if total_invest else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Open Legs", open_pos)
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


format_dict = {
    'entry': '{:.2f}', 'LTP': '{:.2f}', 'exit': '{:.2f}', 'points': '{:.2f}',
    'invest': '{:,.0f}', 'profit': '{:,.0f}', 'Net Invest': '{:,.0f}',
    'Net Profit': '{:,.0f}', 'profit%': '{:.1f}%'
}

styled = (
    show_df.style
    .map(color_pnl, subset=['points', 'profit', 'Net Profit', 'profit%'])
    .set_properties(subset=['STRATEGY'], **{'background-color': '#c6efce'})
    .set_properties(subset=['SYMBOL'], **{'background-color': '#dbeeff'})
    .format(format_dict, na_rep='—')
    .set_properties(**{'text-align': 'center', 'font-size': '15px'})
)

st.caption(f"Last Updated: {get_ist_now().strftime('%H:%M:%S')} IST")
st.dataframe(styled, hide_index=True, width='stretch', height=min(600, 60 + 40 * len(show_df)))

if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
