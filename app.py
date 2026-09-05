import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import requests
import os
import json
import time
import gzip
import shutil
import concurrent.futures
import html as html_lib
import io
from datetime import datetime

from storage import (
    get_ist_now, DATA_DIR, PERSISTENCE_CONFIGURED,
    load_positions, save_positions, next_sno, esc, pnl_style,
)

# ============================================================
# SIMPLE BY DESIGN
#
# Every position here is exactly one hedge: one symbol, a CE leg and a PE
# leg. No spreadsheet-style editable grid — that's what kept breaking
# (Streamlit's st.data_editor has real, reproducible bugs around dynamic
# rows and date/number columns). Instead: fill a form to open a position,
# fill a small form to close it. Plain widgets, no fragile grid.
#
# This is Page 1 of a 2-page app. Page 2 ("Calculator" in the sidebar
# nav, pages/1_Calculator.py) is a fully separate, fully manual what-if
# calculator — no live positions, no API — for checking risk:reward
# before you actually place a trade. Persistence (GitHub Gist) setup
# instructions live in storage.py.
# ============================================================
st.set_page_config(page_title="PNL Tracker", page_icon="📈", layout="wide")

st.markdown("""
    <style>
        .block-container { padding-top: 1rem !important; padding-bottom: 1rem !important; }
        h1 { font-size: 1.8rem !important; margin-bottom: 0.3rem !important; }
        div[data-testid="stDataFrame"] { font-weight: 600 !important; }
    </style>
""", unsafe_allow_html=True)

TOKEN_FILE = os.path.join(DATA_DIR, 'token.json')
LTP_CACHE_FILE = os.path.join(DATA_DIR, 'ltp_cache.json')
TELEGRAM_CONFIG_FILE = os.path.join(DATA_DIR, 'telegram_config.json')
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
# Telegram alerts
# ============================================================
def load_telegram_config():
    if os.path.exists(TELEGRAM_CONFIG_FILE):
        try:
            with open(TELEGRAM_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {'bot_token': '', 'chat_id': '', 'enabled': False}


def save_telegram_config(cfg):
    try:
        with open(TELEGRAM_CONFIG_FILE, 'w') as f:
            json.dump(cfg, f)
    except Exception:
        pass


def send_telegram_message(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return False, "Bot Token / Chat ID missing"
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(url, data={'chat_id': chat_id, 'text': text}, timeout=8)
        if resp.status_code == 200:
            return True, "ok"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


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
    Resolved ONCE when a position is opened (or restored) and then stored
    with it — a live position's contract doesn't need to keep
    re-resolving itself.
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
# Sidebar
# ============================================================
with st.sidebar:
    if not PERSISTENCE_CONFIGURED:
        st.error(
            "⚠️ No durable storage configured. Positions are saved to this "
            "container's local disk only and **will be lost** the next time "
            "the app restarts (redeploy, sleep/wake, or platform recycle) — "
            "this is what wiped your positions before. Add GITHUB_TOKEN and "
            "GIST_ID under Settings → Secrets to fix this permanently. "
            "See the comment block at the top of storage.py for the 2-minute setup."
        )
    else:
        st.success("✅ Durable storage active (GitHub Gist). Positions survive app restarts.")

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
    st.header("Telegram Alerts")
    tg_cfg = load_telegram_config()
    tg_bot_token = st.text_input("Bot Token", value=tg_cfg.get('bot_token', ''), type="password")
    tg_chat_id = st.text_input("Chat ID", value=tg_cfg.get('chat_id', ''))
    tg_enabled = st.checkbox(
        "Enable Alerts (TGT hit, profit% ≥ 50%, profit% ≤ -30%)",
        value=tg_cfg.get('enabled', False)
    )
    if (tg_bot_token, tg_chat_id, tg_enabled) != (tg_cfg.get('bot_token', ''), tg_cfg.get('chat_id', ''), tg_cfg.get('enabled', False)):
        save_telegram_config({'bot_token': tg_bot_token, 'chat_id': tg_chat_id, 'enabled': tg_enabled})
    if st.button("Send Test Message", use_container_width=True):
        ok, msg = send_telegram_message(tg_bot_token, tg_chat_id, "✅ Hedge PNL Tracker: test alert.")
        st.success("Sent.") if ok else st.error(f"Failed: {msg}")

    st.markdown("---")
    st.header("Auto Refresh")
    auto_refresh = st.checkbox("Enable Auto-Refresh", value=False)
    refresh_interval = st.slider("Refresh Interval (seconds)", min_value=5, max_value=60, value=15)

    st.markdown("---")
    if st.button("🔧 Re-resolve missing contract keys", use_container_width=True):
        today_str = get_ist_now().strftime('%Y-%m-%d')
        current_positions = load_positions()
        fixed = 0
        for p in current_positions:
            for leg in ('ce', 'pe'):
                if float(p.get(f'{leg}_entry') or 0) > 0 and not p.get(f'{leg}_instrument_key'):
                    key, lot_size, expiry_str = resolve_current_contract(
                        p['symbol'], p[f'{leg}_strike'], leg.upper(), today_str
                    )
                    if key:
                        p[f'{leg}_instrument_key'] = key
                        if lot_size and not p.get('lot_size'):
                            p['lot_size'] = lot_size
                        if expiry_str and not p.get('expiry'):
                            p['expiry'] = expiry_str
                        fixed += 1
        if fixed:
            save_positions(current_positions)
            st.success(f"Resolved {fixed} missing contract key(s).")
            time.sleep(1)
            st.rerun()
        else:
            st.info("Nothing to fix — download NSE.json first if keys are still missing.")

# ============================================================
# Main page
# ============================================================
st.title("PNL Tracker")

positions = load_positions()

# ------------------------------------------------------------
# Open a new position
# ------------------------------------------------------------
with st.expander("➕ Add Position", expanded=(len(positions) == 0)):
    st.caption("Only taking one side? Leave the other leg's Entry at 0 — it won't be calculated or shown as open.")
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
            # A leg only counts as "taken" if it has an entry price. Some
            # hedges are CE-only or PE-only — a leg with entry left at 0 is
            # simply not part of the position and is never calculated.
            ce_taken = ce_entry > 0
            pe_taken = pe_entry > 0
            valid = (
                bool(symbol) and (ce_taken or pe_taken)
                and (not ce_taken or ce_strike > 0)
                and (not pe_taken or pe_strike > 0)
            )
            if not valid:
                st.error(
                    "Symbol is required, plus at least one leg (CE or PE) with both its "
                    "strike and entry price filled in — the other leg can be left at 0 "
                    "if this hedge only has one side."
                )
            else:
                today_str = get_ist_now().strftime('%Y-%m-%d')
                ce_key = lot_size = expiry_str = None
                pe_key = pe_lot_size = pe_expiry_str = None
                if ce_taken:
                    ce_key, lot_size, expiry_str = resolve_current_contract(symbol, ce_strike, "CE", today_str)
                if pe_taken:
                    pe_key, pe_lot_size, pe_expiry_str = resolve_current_contract(symbol, pe_strike, "PE", today_str)
                lot_size = lot_size or pe_lot_size
                expiry_str = expiry_str or pe_expiry_str
                if (ce_taken and not ce_key) or (pe_taken and not pe_key):
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
                    'ce_strike': ce_strike if ce_taken else 0,
                    'ce_entry': ce_entry if ce_taken else 0,
                    'ce_tgt': ce_tgt if ce_taken else 0,
                    'ce_qty': int(ce_qty) if ce_taken else 0, 'ce_exit': None,
                    'ce_instrument_key': ce_key if ce_taken else None,
                    'pe_strike': pe_strike if pe_taken else 0,
                    'pe_entry': pe_entry if pe_taken else 0,
                    'pe_tgt': pe_tgt if pe_taken else 0,
                    'pe_qty': int(pe_qty) if pe_taken else 0, 'pe_exit': None,
                    'pe_instrument_key': pe_key if pe_taken else None,
                    'exit_date': None,
                    'remarks': remarks,
                }
                positions.append(new_pos)
                save_positions(positions)
                st.success(f"Added S.no {new_pos['sno']} — {symbol}")
                st.rerun()

if not positions:
    st.info("No positions yet. Use **Add Position** above to open your first hedge, or **Restore from Excel Backup** in the sidebar.")
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
# Date, Symbol, lot Size, Net Invest, Net Profit, profit%, TGT %, Exit
# Date, Remarks) merged (rowspan) across the two leg rows instead of
# repeated.
#
# TGT % is a what-if column: the overall profit% you'd land on if every
# taken leg's TGT were hit exactly, instead of today's LTP/actual exit —
# a quick "how good is my target really" check next to the real profit%.
# ------------------------------------------------------------
open_legs = 0
total_invest = 0.0
total_profit = 0.0
alerts_changed = False
alert_failures = []
enriched = []  # one entry per position, computed once, then filtered/sorted/rendered

for p in positions:
    lot = p.get('lot_size') or 0
    leg_calc = {}
    for leg in ('ce', 'pe'):
        entry = float(p.get(f'{leg}_entry') or 0)
        taken = entry > 0
        if not taken:
            # This leg was never taken (some hedges are CE-only or
            # PE-only) — it contributes nothing to invest/profit/points
            # and doesn't count as an open or closed leg.
            leg_calc[leg] = {
                'strike': p.get(f'{leg}_strike') or 0, 'qty': int(p.get(f'{leg}_qty') or 0),
                'entry': 0.0, 'ltp': 0.0, 'tgt': 0.0, 'exit': None,
                'points': 0.0, 'invest': 0.0, 'profit': 0.0,
                'tgt_profit': 0.0,
                'is_open': False, 'tgt_hit': False, 'taken': False,
            }
            continue
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
        # What-if: profit this leg would show if TGT is hit exactly.
        # Only meaningful when a TGT price has actually been set.
        tgt_profit = ((tgt - entry) * qty * lot) if tgt > 0 else 0.0
        leg_calc[leg] = {
            'strike': p[f'{leg}_strike'], 'qty': qty, 'entry': entry, 'ltp': ltp,
            'tgt': tgt, 'exit': exit_, 'points': points, 'invest': invest,
            'profit': profit, 'tgt_profit': tgt_profit, 'is_open': is_open,
            'tgt_hit': tgt > 0 and is_open and ltp >= tgt, 'taken': True,
        }

    net_invest = leg_calc['ce']['invest'] + leg_calc['pe']['invest']
    net_profit = leg_calc['ce']['profit'] + leg_calc['pe']['profit']
    if net_profit == 0:
        net_profit = 0.0  # avoid displaying "-0"
    net_pct = (net_profit / net_invest * 100) if net_invest else 0.0
    net_tgt_profit = leg_calc['ce']['tgt_profit'] + leg_calc['pe']['tgt_profit']
    tgt_pct = (net_tgt_profit / net_invest * 100) if net_invest else 0.0
    pos_open_legs = int(leg_calc['ce']['is_open']) + int(leg_calc['pe']['is_open'])

    open_legs += pos_open_legs
    total_invest += net_invest
    total_profit += net_profit

    # --- Telegram alerts: TGT hit / profit% >= 50 / profit% <= -30 ---
    # Only actually fires (and only then marks itself "consumed") once
    # Alerts are enabled — leaving alerts off never burns an alert flag,
    # so turning them on later still fires for a condition already true.
    # A Save in the edit form clears the flags so alerts can fire again
    # after the numbers change.
    if tg_enabled:
        for leg in ('ce', 'pe'):
            lc = leg_calc[leg]
            if lc['tgt_hit'] and not p.get(f'{leg}_tgt_alerted'):
                ok, msg = send_telegram_message(
                    tg_bot_token, tg_chat_id,
                    f"🎯 TGT HIT — {p['symbol']} {lc['strike']:.0f} {leg.upper()} "
                    f"(S.no {p['sno']})\nLTP {lc['ltp']:.2f} reached target {lc['tgt']:.2f}"
                )
                if not ok:
                    alert_failures.append(msg)
                p[f'{leg}_tgt_alerted'] = True
                alerts_changed = True
        if pos_open_legs > 0:
            if net_pct >= 50 and not p.get('profit50_alerted'):
                ok, msg = send_telegram_message(
                    tg_bot_token, tg_chat_id,
                    f"🚀 PROFIT ≥ 50% — {p['symbol']} (S.no {p['sno']})\n"
                    f"Net Profit ₹{net_profit:,.0f} | PNL {net_pct:.1f}%"
                )
                if not ok:
                    alert_failures.append(msg)
                p['profit50_alerted'] = True
                alerts_changed = True
            if net_pct <= -30 and not p.get('loss30_alerted'):
                ok, msg = send_telegram_message(
                    tg_bot_token, tg_chat_id,
                    f"⚠️ EXIT? PNL ≤ -30% — {p['symbol']} (S.no {p['sno']})\n"
                    f"Net Profit ₹{net_profit:,.0f} | PNL {net_pct:.1f}%"
                )
                if not ok:
                    alert_failures.append(msg)
                p['loss30_alerted'] = True
                alerts_changed = True

    entry_date_parsed = pd.to_datetime(p.get('entry_date'), errors='coerce')
    exit_date_parsed = pd.to_datetime(p.get('exit_date'), errors='coerce')
    entry_date_str = entry_date_parsed.strftime('%d-%m-%Y') if pd.notna(entry_date_parsed) else '—'
    exit_date_str = exit_date_parsed.strftime('%d-%m-%Y') if pd.notna(exit_date_parsed) else '—'

    enriched.append({
        'p': p, 'leg_calc': leg_calc,
        'net_invest': net_invest, 'net_profit': net_profit, 'net_pct': net_pct,
        'net_tgt_profit': net_tgt_profit, 'tgt_pct': tgt_pct,
        'is_open': pos_open_legs > 0,
        'entry_date_str': entry_date_str, 'exit_date_str': exit_date_str,
        'entry_date_sort': entry_date_parsed, 'exit_date_sort': exit_date_parsed,
    })

overall_pct = (total_profit / total_invest * 100) if total_invest else 0.0

if alerts_changed:
    save_positions(positions)
if alert_failures:
    st.warning("Telegram alert failed to send: " + "; ".join(alert_failures[:3]))

def _colored_metric(label, value_str, val):
    color = '#0b6623' if val > 0 else ('#c0392b' if val < 0 else 'inherit')
    st.markdown(
        f'<div style="font-size:0.875rem;color:rgba(49,51,63,0.6);line-height:1.2;">{esc(label)}</div>'
        f'<div style="font-size:2.25rem;font-weight:600;color:{color};line-height:1.3;">{esc(value_str)}</div>',
        unsafe_allow_html=True,
    )


m1, m2, m3, m4 = st.columns(4)
m1.metric("Open Legs", open_legs)
m2.metric("Total Invested", f"₹{total_invest:,.0f}")
with m3:
    _colored_metric("Total Net Profit", f"₹{total_profit:,.0f}", total_profit)
with m4:
    _colored_metric("Overall PNL %", f"{overall_pct:.1f}%", overall_pct)

# ------------------------------------------------------------
# Interactive table — every column header is clickable to sort (click
# again to reverse), plus an instant search box. Both run entirely in
# the browser (no server round-trip), and open positions are ALWAYS
# kept above closed ones no matter which column is sorted or in what
# direction — that grouping is enforced first, the clicked column only
# orders within each group.
#
# Leg-specific columns (Strike/Qty/entry/LTP/TGT/exit/points/invest/
# profit) show two values per position (CE and PE) — clicking one of
# those headers sorts by the CE leg's value; there's a tooltip on those
# headers saying so.
# ------------------------------------------------------------
def _leg_row_dict(p, lot, leg, lc, entry_date_str, exit_date_str, net_invest, net_profit, net_pct, tgt_pct):
    if not lc.get('taken', True):
        return {
            'S.no': p['sno'], 'Entry Date': entry_date_str, 'SYMBOL': p['symbol'], 'lot Size': lot,
            'Strike': f'{leg.upper()} not taken', 'Qty': '—',
            'entry': '—', 'LTP': '—', 'TGT': '—', 'exit': '—',
            'points': '—', 'invest': '—', 'profit': '—',
            'Net Invest': round(net_invest, 2), 'Net Profit': round(net_profit, 2),
            'profit%': round(net_pct, 2), 'TGT %': round(tgt_pct, 2), 'Exit Date': exit_date_str,
            'remarks': p.get('remarks') or '',
        }
    exit_disp = f"{lc['exit']:.2f}" if lc['exit'] is not None else '—'
    return {
        'S.no': p['sno'], 'Entry Date': entry_date_str, 'SYMBOL': p['symbol'], 'lot Size': lot,
        'Strike': f"{lc['strike']:.0f} {leg.upper()}", 'Qty': lc['qty'],
        'entry': lc['entry'], 'LTP': lc['ltp'], 'TGT': lc['tgt'], 'exit': exit_disp,
        'points': round(lc['points'], 2), 'invest': round(lc['invest'], 2),
        'profit': round(lc['profit'], 2),
        'Net Invest': round(net_invest, 2), 'Net Profit': round(net_profit, 2),
        'profit%': round(net_pct, 2), 'TGT %': round(tgt_pct, 2), 'Exit Date': exit_date_str,
        'remarks': p.get('remarks') or '',
    }


def _ts_ms(ts):
    return int(ts.timestamp() * 1000) if pd.notna(ts) else None


TABLE_COLUMNS = [
    ("S.no", "sno", None),
    ("Entry Date", "entry_date", None),
    ("SYMBOL", "symbol", None),
    ("lot Size", "lot_size", None),
    ("Strike", "ce_strike", "Sorts by the CE leg's value"),
    ("Qty", "ce_qty", "Sorts by the CE leg's value"),
    ("entry", "ce_entry", "Sorts by the CE leg's value"),
    ("LTP", "ce_ltp", "Sorts by the CE leg's value"),
    ("TGT", "ce_tgt", "Sorts by the CE leg's value"),
    ("exit", "ce_exit", "Sorts by the CE leg's value"),
    ("points", "ce_points", "Sorts by the CE leg's value"),
    ("invest", "ce_invest", "Sorts by the CE leg's value"),
    ("profit", "ce_profit", "Sorts by the CE leg's value"),
    ("Net Invest", "net_invest", None),
    ("Net Profit", "net_profit", None),
    ("profit%", "net_pct", None),
    ("TGT %", "tgt_pct", "Profit % you'd land on if every taken leg's TGT is hit exactly"),
    ("Exit Date", "exit_date", None),
    ("remarks", "remarks", None),
]

# Fixed initial order in the DOM: open positions first (by S.no), then
# closed (by S.no). The script re-sorts client-side from here, but always
# re-applies this same open-before-closed grouping after every click.
initial_open = sorted((e for e in enriched if e['is_open']), key=lambda e: e['p']['sno'])
initial_closed = sorted((e for e in enriched if not e['is_open']), key=lambda e: e['p']['sno'])
initial_order = initial_open + initial_closed

body_blocks_html = []
for pos_idx, e in enumerate(initial_order):
    p, leg_calc = e['p'], e['leg_calc']
    net_invest, net_profit, net_pct = e['net_invest'], e['net_profit'], e['net_pct']
    net_tgt_profit, tgt_pct = e['net_tgt_profit'], e['tgt_pct']
    entry_date_str, exit_date_str = e['entry_date_str'], e['exit_date_str']
    lot = p.get('lot_size') or 0
    ce = leg_calc['ce']

    sort_vals = {
        'sno': p['sno'],
        'entry_date': _ts_ms(e['entry_date_sort']),
        'symbol': p['symbol'],
        'lot_size': lot,
        'net_invest': round(net_invest, 2),
        'net_profit': round(net_profit, 2),
        'net_pct': round(net_pct, 2),
        'tgt_pct': round(tgt_pct, 2),
        'exit_date': _ts_ms(e['exit_date_sort']),
        'remarks': p.get('remarks') or '',
        'ce_strike': ce['strike'],
        'ce_qty': ce['qty'],
        'ce_entry': ce['entry'],
        'ce_ltp': ce['ltp'],
        'ce_tgt': ce['tgt'],
        'ce_exit': ce['exit'],
        'ce_points': round(ce['points'], 2),
        'ce_invest': round(ce['invest'], 2),
        'ce_profit': round(ce['profit'], 2),
    }
    vals_attr = html_lib.escape(json.dumps(sort_vals), quote=True)

    # Closed positions get their ENTIRE row colored by outcome (green =
    # profit, red = loss) instead of the usual alternating white/grey
    # banding — a closed position is done, so its row should read as a
    # single win/loss at a glance. Open positions keep the normal banding
    # plus per-cell highlights (entry/exit/LTP/points/profit colors).
    closed_class = None
    if not e['is_open']:
        if net_profit > 0:
            closed_class = 'row-closed-profit'
        elif net_profit < 0:
            closed_class = 'row-closed-loss'
    band = closed_class or ('row-band-b' if pos_idx % 2 else 'row-band-a')

    rows = []
    for i, leg in enumerate(('ce', 'pe')):
        lc = leg_calc[leg]
        sym_extra = '' if closed_class else ' sym-cell'
        entry_extra = '' if closed_class else ' entry-cell'
        exit_extra = '' if closed_class else ' exit-cell'
        ltp_style = '' if closed_class else ('background-color:#0b6623;color:#fff;font-weight:700' if lc['tgt_hit'] else '')
        points_style = '' if closed_class else pnl_style(lc["points"])
        profit_style = '' if closed_class else pnl_style(lc["profit"])
        net_profit_style = '' if closed_class else pnl_style(net_profit)
        exit_disp = f"{lc['exit']:.2f}" if lc['exit'] is not None else '—'

        cells = []
        if i == 0:
            cells.append(f'<td rowspan="2" class="{band}">{p["sno"]}</td>')
            cells.append(f'<td rowspan="2" class="{band}">{entry_date_str}</td>')
            cells.append(f'<td rowspan="2" class="{band}{sym_extra}">{esc(p["symbol"])}</td>')
            cells.append(f'<td rowspan="2" class="{band}">{lot}</td>')
        if not lc.get('taken', True):
            cells.append(
                f'<td class="{band}" colspan="9" style="color:#888;font-style:italic;background:#f2f2f2;">'
                f'{leg.upper()} leg not taken</td>'
            )
        else:
            cells.append(f'<td class="{band}">{lc["strike"]:.0f} {leg.upper()}</td>')
            cells.append(f'<td class="{band}">{lc["qty"]}</td>')
            cells.append(f'<td class="{band}{entry_extra}">{lc["entry"]:.2f}</td>')
            cells.append(f'<td class="{band}" style="{ltp_style}">{lc["ltp"]:.2f}</td>')
            cells.append(f'<td class="{band}">{lc["tgt"]:.2f}</td>')
            cells.append(f'<td class="{band}{exit_extra}">{exit_disp}</td>')
            cells.append(f'<td class="{band}" style="{points_style}">{lc["points"]:.2f}</td>')
            cells.append(f'<td class="{band}">{lc["invest"]:,.0f}</td>')
            cells.append(f'<td class="{band}" style="{profit_style}">{lc["profit"]:,.0f}</td>')
        if i == 0:
            cells.append(f'<td rowspan="2" class="{band}">{net_invest:,.0f}</td>')
            cells.append(f'<td rowspan="2" class="{band}" style="{net_profit_style}">{net_profit:,.0f}</td>')
            cells.append(f'<td rowspan="2" class="{band}" style="{net_profit_style}">{net_pct:.1f}%</td>')
            cells.append(f'<td rowspan="2" class="{band}">{tgt_pct:.1f}%</td>')
            cells.append(f'<td rowspan="2" class="{band}">{exit_date_str}</td>')
            cells.append(f'<td rowspan="2" class="{band}">{esc(p.get("remarks") or "")}</td>')
        rows.append('<tr>' + ''.join(cells) + '</tr>')

    body_blocks_html.append(
        f'<tbody data-open="{1 if e["is_open"] else 0}" data-vals="{vals_attr}">'
        + ''.join(rows) + '</tbody>'
    )

header_cells = []
for label, key, tooltip in TABLE_COLUMNS:
    title_attr = f' title="{esc(tooltip)}"' if tooltip else ''
    header_cells.append(f'<th data-key="{key}" data-label="{esc(label)}"{title_attr}>{esc(label)}</th>')

# Excel export — deliberately covers EVERY position regardless of the
# on-screen search box, so "Download as Excel" always stays a full
# backup no matter what's currently filtered/sorted on screen. (This
# also matters because a previous version of this app relied on that
# same download to recover from a data-loss incident.)
export_rows = []
for e in initial_order:
    p, leg_calc = e['p'], e['leg_calc']
    lot = p.get('lot_size') or 0
    for leg in ('ce', 'pe'):
        export_rows.append(_leg_row_dict(
            p, lot, leg, leg_calc[leg], e['entry_date_str'], e['exit_date_str'],
            e['net_invest'], e['net_profit'], e['net_pct'], e['tgt_pct']
        ))

st.caption(f"Last Updated: {get_ist_now().strftime('%H:%M:%S')} IST")

table_page_html = f"""
<style>
    body {{ margin:0; font-family: "Source Sans Pro", sans-serif; }}
    #searchBox {{
        width: 100%; box-sizing: border-box; padding: 8px 12px; margin-bottom: 8px;
        border: 1px solid #d0d0d0; border-radius: 6px; font-size: 14px;
    }}
    .pnl-table-wrap {{ overflow: auto; max-height: 1050px; border: 1px solid #d0d0d0; border-radius: 6px; }}
    table.pnl-table {{ border-collapse: collapse; width: 100%; font-size: 14px; white-space: nowrap; }}
    table.pnl-table th, table.pnl-table td {{
        border: 1px solid #d0d0d0; padding: 6px 10px; text-align: center;
    }}
    table.pnl-table thead th {{
        background-color: #f4a261; color: #1a1a1a; font-weight: 700;
        position: sticky; top: 0; z-index: 1; cursor: pointer; user-select: none;
    }}
    table.pnl-table thead th:hover {{ background-color: #f0954a; }}
    table.pnl-table .row-band-a {{ background-color: #ffffff; }}
    table.pnl-table .row-band-b {{ background-color: #f7f9fb; }}
    table.pnl-table .row-closed-profit {{ background-color: #d4edda; }}
    table.pnl-table .row-closed-loss {{ background-color: #f8d7da; }}
    table.pnl-table .sym-cell {{ background-color: #dbeeff !important; font-weight: 700; color: #0b3d91; }}
    table.pnl-table .entry-cell {{ background-color: #c6efce; font-weight: 600; }}
    table.pnl-table .exit-cell {{ background-color: #ffeb9c; font-weight: 600; }}
    #noMatch {{ padding: 10px; color: #555; font-style: italic; display: none; }}
</style>

<input id="searchBox" type="text" placeholder="🔍 Search S.no, symbol or remarks..." />
<div class="pnl-table-wrap">
<table class="pnl-table" id="pnlTable">
<thead>
<tr>
{''.join(header_cells)}
</tr>
</thead>
{''.join(body_blocks_html)}
</table>
</div>
<div id="noMatch">No positions match your search.</div>

<script>
(function() {{
    var table = document.getElementById('pnlTable');
    var currentSort = {{ key: null, dir: 1 }};

    function cmp(a, b) {{
        if (a === null || a === undefined) a = -Infinity;
        if (b === null || b === undefined) b = -Infinity;
        if (typeof a === 'string' && typeof b === 'string') return a.localeCompare(b);
        return a - b;
    }}

    function applySort(key) {{
        if (currentSort.key === key) {{ currentSort.dir *= -1; }} else {{ currentSort = {{ key: key, dir: 1 }}; }}
        var bodies = Array.from(table.querySelectorAll('tbody'));
        bodies.sort(function(ta, tb) {{
            var openA = ta.dataset.open === '1', openB = tb.dataset.open === '1';
            if (openA !== openB) return openA ? -1 : 1;
            var va = JSON.parse(ta.dataset.vals), vb = JSON.parse(tb.dataset.vals);
            return cmp(va[key], vb[key]) * currentSort.dir;
        }});
        bodies.forEach(function(tb) {{ table.appendChild(tb); }});
        document.querySelectorAll('th[data-key]').forEach(function(th) {{
            var base = th.dataset.label;
            th.textContent = (th.dataset.key === key) ? (base + (currentSort.dir === 1 ? ' \\u25B2' : ' \\u25BC')) : base;
        }});
    }}

    document.querySelectorAll('th[data-key]').forEach(function(th) {{
        th.addEventListener('click', function() {{ applySort(th.dataset.key); }});
    }});

    document.getElementById('searchBox').addEventListener('input', function() {{
        var term = this.value.trim().toUpperCase();
        var visible = 0;
        document.querySelectorAll('#pnlTable tbody').forEach(function(tb) {{
            var vals = JSON.parse(tb.dataset.vals);
            var hay = (vals.symbol + ' ' + (vals.remarks || '') + ' ' + vals.sno).toUpperCase();
            var show = !term || hay.indexOf(term) !== -1;
            tb.style.display = show ? '' : 'none';
            if (show) visible++;
        }});
        document.getElementById('noMatch').style.display = (term && visible === 0) ? 'block' : 'none';
    }});
}})();
</script>
"""

components.html(table_page_html, height=1130, scrolling=True)

# ------------------------------------------------------------
# Excel download + clear-all
# ------------------------------------------------------------
dl_col, clear_col = st.columns(2)

with dl_col:
    export_buf = io.BytesIO()
    pd.DataFrame(export_rows).to_excel(export_buf, index=False, sheet_name='Positions', engine='openpyxl')
    st.download_button(
        "⬇️ Download as Excel",
        data=export_buf.getvalue(),
        file_name=f"hedge_positions_{get_ist_now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

with clear_col:
    with st.popover("🗑️ Clear All Positions", use_container_width=True):
        st.warning("This deletes every position permanently. This cannot be undone.")
        confirm_clear = st.checkbox("Yes, I'm sure — clear everything")
        if st.button("Confirm Clear All", disabled=not confirm_clear, use_container_width=True):
            save_positions([])
            st.success("Cleared.")
            time.sleep(1)
            st.rerun()

# ------------------------------------------------------------
# Close / edit a position — plain widgets, no grid editing. Collapsed
# by default (like Add Position) so the table above gets the vertical
# space instead of this form sitting open all the time.
# ------------------------------------------------------------
with st.expander("✏️ Close / Edit a Position", expanded=False):
    options = {f"S.no {p['sno']} — {p['symbol']}": p['sno'] for p in positions}
    choice = st.selectbox("Position", options=list(options.keys()))
    sel_sno = options[choice]
    pos = next(p for p in positions if p['sno'] == sel_sno)

    with st.form("edit_position_form"):
        st.caption("Entry Date & Strikes — fix these here if they were entered wrong")
        d1, d2, d3 = st.columns(3)
        entry_date_edit_val = d1.date_input(
            "Entry Date",
            value=pd.to_datetime(pos.get('entry_date')).date() if pos.get('entry_date') else get_ist_now().date()
        )
        ce_strike_val = d2.number_input(
            "CE Strike", min_value=0.0, step=0.5, format="%.1f",
            value=float(pos.get('ce_strike') or 0.0)
        )
        pe_strike_val = d3.number_input(
            "PE Strike", min_value=0.0, step=0.5, format="%.1f",
            value=float(pos.get('pe_strike') or 0.0)
        )

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

        st.caption("TGT")
        t1, t2 = st.columns(2)
        ce_tgt_val = t1.number_input(
            "CE TGT", min_value=0.0, step=0.05, format="%.2f",
            value=float(pos.get('ce_tgt') or 0.0)
        )
        pe_tgt_val = t2.number_input(
            "PE TGT", min_value=0.0, step=0.05, format="%.2f",
            value=float(pos.get('pe_tgt') or 0.0)
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
            original_ce_strike = pos.get('ce_strike') or 0
            original_pe_strike = pos.get('pe_strike') or 0

            pos['entry_date'] = str(entry_date_edit_val)
            pos['ce_strike'] = ce_strike_val
            pos['pe_strike'] = pe_strike_val
            pos['ce_entry'] = ce_entry_val
            pos['pe_entry'] = pe_entry_val
            pos['ce_qty'] = int(ce_qty_val)
            pos['pe_qty'] = int(pe_qty_val)
            pos['ce_tgt'] = ce_tgt_val
            pos['pe_tgt'] = pe_tgt_val
            pos['ce_exit'] = ce_exit_val if ce_exit_val > 0 else None
            pos['pe_exit'] = pe_exit_val if pe_exit_val > 0 else None
            pos['exit_date'] = str(exit_date_val) if (ce_exit_val > 0 or pe_exit_val > 0) else None
            pos['remarks'] = remarks_val

            # A corrected strike means the instrument key resolved earlier
            # is for the WRONG contract and would keep showing that
            # contract's LTP — re-resolve whichever leg's strike actually
            # changed (only matters for a leg that's actually taken).
            today_str = get_ist_now().strftime('%Y-%m-%d')
            if pos['ce_entry'] > 0 and ce_strike_val != original_ce_strike:
                ce_key, ce_lot, ce_expiry = resolve_current_contract(pos['symbol'], ce_strike_val, "CE", today_str)
                pos['ce_instrument_key'] = ce_key
                if ce_lot:
                    pos['lot_size'] = ce_lot
                if ce_expiry:
                    pos['expiry'] = ce_expiry
                if not ce_key:
                    st.warning("Couldn't match the new CE strike to a live contract — download NSE.json first.")
            if pos['pe_entry'] > 0 and pe_strike_val != original_pe_strike:
                pe_key, pe_lot, pe_expiry = resolve_current_contract(pos['symbol'], pe_strike_val, "PE", today_str)
                pos['pe_instrument_key'] = pe_key
                if pe_lot and not pos.get('lot_size'):
                    pos['lot_size'] = pe_lot
                if pe_expiry and not pos.get('expiry'):
                    pos['expiry'] = pe_expiry
                if not pe_key:
                    st.warning("Couldn't match the new PE strike to a live contract — download NSE.json first.")

            # Numbers changed — let TGT/profit% alerts re-evaluate from scratch.
            for flag in ('ce_tgt_alerted', 'pe_tgt_alerted', 'profit50_alerted', 'loss30_alerted'):
                pos.pop(flag, None)
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
