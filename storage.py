"""
Shared persistence + small HTML-table helpers used by BOTH pages of this
app (app.py = "Hedge PNL Tracker", pages/1_Calculator.py = "Exit Decision
Calculator"). Kept in one module so both pages talk to the exact same
GitHub Gist config and JSON schema instead of drifting apart.

PERSISTENCE
Streamlit Community Cloud containers are EPHEMERAL — anything written
only to local disk is wiped on the next container restart (redeploy,
sleep/wake, or platform recycle). So positions and decision rows are
mirrored to a private GitHub Gist (a tiny free JSON store outside the
container) whenever GITHUB_TOKEN + GIST_ID are set in Streamlit secrets.
The local file is kept too, purely as a fast read cache — the Gist is
the source of truth. Without those two secrets configured, the app still
runs (local file only) but data WILL be lost on the next restart.

Setup (one-time, ~2 minutes):
  1. github.com/settings/tokens -> Generate new token (classic) -> only
     the "gist" scope -> copy it.
  2. gist.github.com -> New secret gist -> add a file named
     "hedge_positions.json" with content "[]" -> Create secret gist ->
     copy the gist ID from its URL. (A second file "decision_table.json"
     in the SAME gist is created automatically the first time you save a
     decision row — no need to add it by hand.)
  3. In your Streamlit Cloud app -> Settings -> Secrets, add:
       GITHUB_TOKEN = "ghp_xxx..."
       GIST_ID = "the id from step 2"
  4. Reboot the app once from Streamlit Cloud's menu.
"""
import html as html_lib
import json
import os
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

# ============================================================
# IST helpers
# ============================================================
IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def get_ist_now():
    return datetime.now(IST)


# ============================================================
# Durable storage config
# ============================================================
DATA_DIR = 'data'
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

POSITIONS_FILE = os.path.join(DATA_DIR, 'hedge_positions.json')
DECISION_FILE = os.path.join(DATA_DIR, 'decision_table.json')

GITHUB_TOKEN = st.secrets.get("GITHUB_TOKEN", "") if hasattr(st, "secrets") else ""
GIST_ID = st.secrets.get("GIST_ID", "") if hasattr(st, "secrets") else ""
POSITIONS_GIST_FILENAME = "hedge_positions.json"
DECISION_GIST_FILENAME = "decision_table.json"
PERSISTENCE_CONFIGURED = bool(GITHUB_TOKEN and GIST_ID)


def _gist_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def _gist_read_file(filename):
    """Returns a list from the named file inside the gist, or None if the
    gist isn't configured or couldn't be reached (caller should fall back
    to its local cache file)."""
    if not PERSISTENCE_CONFIGURED:
        return None
    try:
        r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=10)
        if r.status_code != 200:
            st.sidebar.error(f"Gist load failed: HTTP {r.status_code}")
            return None
        files = r.json().get("files", {})
        f = files.get(filename)
        if not f:
            return []
        content = f.get("content", "") or ""
        if f.get("truncated"):
            raw = requests.get(f["raw_url"], headers=_gist_headers(), timeout=10)
            content = raw.text
        return json.loads(content) if content.strip() else []
    except Exception as e:
        st.sidebar.error(f"Gist load failed: {e}")
        return None


def _gist_write_file(filename, data):
    if not PERSISTENCE_CONFIGURED:
        return False
    try:
        payload = {"files": {filename: {"content": json.dumps(data, indent=2, default=str)}}}
        r = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), json=payload, timeout=10)
        if r.status_code != 200:
            st.sidebar.error(f"Gist save failed: HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        st.sidebar.error(f"Gist save failed: {e}")
        return False


# ---- Positions (Page 1 — Hedge PNL Tracker) ----
def load_positions():
    remote = _gist_read_file(POSITIONS_GIST_FILENAME)
    if remote is not None:
        try:
            with open(POSITIONS_FILE, 'w') as f:
                json.dump(remote, f, indent=2, default=str)
        except Exception:
            pass
        return remote
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
        st.error(f"Could not save positions locally: {e}")
    if PERSISTENCE_CONFIGURED:
        if not _gist_write_file(POSITIONS_GIST_FILENAME, positions):
            st.sidebar.error("⚠️ Could not reach GitHub Gist backup just now — saved locally only for this session.")


def next_sno(positions):
    if not positions:
        return 1
    return max(p.get('sno', 0) for p in positions) + 1


# ---- Decision rows (Page 2 — Exit Decision Calculator) ----
def load_decision_rows():
    remote = _gist_read_file(DECISION_GIST_FILENAME)
    if remote is not None:
        try:
            with open(DECISION_FILE, 'w') as f:
                json.dump(remote, f, indent=2, default=str)
        except Exception:
            pass
        return remote
    if os.path.exists(DECISION_FILE):
        try:
            with open(DECISION_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_decision_rows(rows):
    try:
        with open(DECISION_FILE, 'w') as f:
            json.dump(rows, f, indent=2, default=str)
    except Exception as e:
        st.error(f"Could not save decision rows locally: {e}")
    if PERSISTENCE_CONFIGURED:
        if not _gist_write_file(DECISION_GIST_FILENAME, rows):
            st.sidebar.error("⚠️ Could not reach GitHub Gist backup just now — saved locally only for this session.")


def next_drow_id(rows):
    if not rows:
        return 1
    return max(r.get('id', 0) for r in rows) + 1


# ---- Shared HTML-table helpers ----
def esc(v):
    return html_lib.escape(str(v))


def pnl_style(val):
    if val > 0:
        return 'background-color:#d4edda;color:#155724;font-weight:700'
    if val < 0:
        return 'background-color:#f8d7da;color:#721c24;font-weight:700'
    return ''
