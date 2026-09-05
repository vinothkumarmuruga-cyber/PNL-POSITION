import streamlit as st

from storage import (
    get_ist_now, PERSISTENCE_CONFIGURED,
    load_decision_rows, save_decision_rows, next_drow_id, esc, pnl_style,
)

# ============================================================
# Exit Decision Calculator — Page 2.
#
# A completely separate, fully manual what-if table. Nothing here
# touches the Upstox API or live LTP: you type in your own entry price
# and a target/exit price for each leg, and points/invest/profit/
# profit% are computed as if you exited exactly at that target — purely
# to help decide (and check your risk:reward) BEFORE you actually place
# a trade on Page 1.
# ============================================================
st.set_page_config(page_title="Exit Decision Calculator", page_icon="📋", layout="wide")

st.markdown("""
    <style>
        .block-container { padding-top: 1rem !important; padding-bottom: 1rem !important; }
        h1 { font-size: 1.8rem !important; margin-bottom: 0.3rem !important; }
    </style>
""", unsafe_allow_html=True)

st.title("📋 Exit Decision Calculator")
st.caption(
    "Fully manual — no API, no live LTP. Enter your entry price and a target exit price for "
    "each leg to see the profit you'd lock in if you exited there — check your risk:reward "
    "before you actually place the trade on the Hedge PNL Tracker page."
)
if not PERSISTENCE_CONFIGURED:
    st.warning(
        "⚠️ No durable storage configured (see storage.py) — rows here are saved to local "
        "disk only and will be lost on the next app restart."
    )

decision_rows = load_decision_rows()

with st.expander("➕ Add Decision Row", expanded=(len(decision_rows) == 0)):
    st.caption("Only one side? Leave that leg's Entry Price at 0 — it's skipped.")
    with st.form("add_decision_form", clear_on_submit=True):
        d_symbol = st.text_input("Symbol", placeholder="e.g. KOTAKBANK", key="d_symbol").strip().upper()
        d_lot = st.number_input("lot Size", min_value=0, step=1, value=0, key="d_lot")

        st.markdown("**CE leg**")
        dce1, dce2, dce3, dce4, dce5 = st.columns(5)
        dce_entry_strike = dce1.number_input("Entry Strike", min_value=0.0, step=0.5, format="%.1f", key="dce_entry_strike")
        dce_entry_price = dce2.number_input("Entry Price", min_value=0.0, step=0.05, format="%.2f", key="dce_entry_price")
        dce_tgt_strike = dce3.number_input("TGT Strike", min_value=0.0, step=0.5, format="%.1f", key="dce_tgt_strike")
        dce_tgt_price = dce4.number_input("TGT Price", min_value=0.0, step=0.05, format="%.2f", key="dce_tgt_price")
        dce_qty = dce5.number_input("Qty", min_value=1, step=1, value=1, key="dce_qty")

        st.markdown("**PE leg**")
        dpe1, dpe2, dpe3, dpe4, dpe5 = st.columns(5)
        dpe_entry_strike = dpe1.number_input("Entry Strike", min_value=0.0, step=0.5, format="%.1f", key="dpe_entry_strike")
        dpe_entry_price = dpe2.number_input("Entry Price", min_value=0.0, step=0.05, format="%.2f", key="dpe_entry_price")
        dpe_tgt_strike = dpe3.number_input("TGT Strike", min_value=0.0, step=0.5, format="%.1f", key="dpe_tgt_strike")
        dpe_tgt_price = dpe4.number_input("TGT Price", min_value=0.0, step=0.05, format="%.2f", key="dpe_tgt_price")
        dpe_qty = dpe5.number_input("Qty", min_value=1, step=1, value=1, key="dpe_qty")

        d_submitted = st.form_submit_button("Add Decision Row", use_container_width=True)

        if d_submitted:
            ce_taken = dce_entry_price > 0
            pe_taken = dpe_entry_price > 0
            if not d_symbol or not (ce_taken or pe_taken):
                st.error("Symbol is required, plus at least one leg's Entry Price filled in.")
            else:
                new_drow = {
                    'id': next_drow_id(decision_rows),
                    'symbol': d_symbol,
                    'lot_size': int(d_lot),
                    'ce_entry_strike': dce_entry_strike if ce_taken else 0,
                    'ce_entry_price': dce_entry_price if ce_taken else 0,
                    'ce_tgt_strike': dce_tgt_strike if ce_taken else 0,
                    'ce_tgt_price': dce_tgt_price if ce_taken else 0,
                    'ce_qty': int(dce_qty) if ce_taken else 0,
                    'pe_entry_strike': dpe_entry_strike if pe_taken else 0,
                    'pe_entry_price': dpe_entry_price if pe_taken else 0,
                    'pe_tgt_strike': dpe_tgt_strike if pe_taken else 0,
                    'pe_tgt_price': dpe_tgt_price if pe_taken else 0,
                    'pe_qty': int(dpe_qty) if pe_taken else 0,
                }
                decision_rows.append(new_drow)
                save_decision_rows(decision_rows)
                st.success(f"Added decision row — {d_symbol}")
                st.rerun()

if decision_rows:
    d_body_rows = []
    for d_idx, r in enumerate(decision_rows):
        lot = r.get('lot_size') or 0
        leg_calc_d = {}
        for leg in ('ce', 'pe'):
            entry_price = float(r.get(f'{leg}_entry_price') or 0)
            taken = entry_price > 0
            if not taken:
                leg_calc_d[leg] = {
                    'entry_strike': 0, 'entry_price': 0.0, 'tgt_strike': 0, 'tgt_price': 0.0,
                    'qty': 0, 'points': 0.0, 'invest': 0.0, 'profit': 0.0, 'taken': False,
                }
                continue
            tgt_price = float(r.get(f'{leg}_tgt_price') or 0)
            qty = int(r.get(f'{leg}_qty') or 0)
            points = (tgt_price - entry_price) * qty
            invest = entry_price * lot * qty
            profit = points * lot
            leg_calc_d[leg] = {
                'entry_strike': r.get(f'{leg}_entry_strike') or 0, 'entry_price': entry_price,
                'tgt_strike': r.get(f'{leg}_tgt_strike') or 0, 'tgt_price': tgt_price,
                'qty': qty, 'points': points, 'invest': invest, 'profit': profit, 'taken': True,
            }

        net_invest_d = leg_calc_d['ce']['invest'] + leg_calc_d['pe']['invest']
        net_profit_d = leg_calc_d['ce']['profit'] + leg_calc_d['pe']['profit']
        if net_profit_d == 0:
            net_profit_d = 0.0
        net_pct_d = (net_profit_d / net_invest_d * 100) if net_invest_d else 0.0

        band = 'row-band-b' if d_idx % 2 else 'row-band-a'
        for i, leg in enumerate(('ce', 'pe')):
            lc = leg_calc_d[leg]
            cells = []
            if i == 0:
                cells.append(f'<td rowspan="2" class="{band} sym-cell">{esc(r["symbol"])}</td>')

            # ENTRY STRIKE + ENTRY PRICE (2 cols) — collapsed to one cell if not taken.
            if not lc['taken']:
                cells.append(
                    f'<td class="{band}" colspan="2" style="color:#888;font-style:italic;background:#f2f2f2;">'
                    f'{leg.upper()} not taken</td>'
                )
            else:
                cells.append(f'<td class="{band}">{lc["entry_strike"]:.0f} {leg.upper()}</td>')
                cells.append(f'<td class="{band} entry-cell">{lc["entry_price"]:.2f}</td>')

            # lot Size sits between the two leg-blocks in the header, so it
            # always gets its own rowspan cell here regardless of taken status.
            if i == 0:
                cells.append(f'<td rowspan="2" class="{band}">{lot}</td>')

            # TGT STRIKE, TGT PRICE, Qty, entry, exit, points, invest, profit (8 cols).
            if not lc['taken']:
                cells.append(f'<td class="{band}" colspan="8" style="background:#f2f2f2;"></td>')
            else:
                cells.append(f'<td class="{band}">{lc["tgt_strike"]:.0f} {leg.upper()}</td>')
                cells.append(f'<td class="{band} exit-cell">{lc["tgt_price"]:.2f}</td>')
                cells.append(f'<td class="{band}">{lc["qty"]}</td>')
                cells.append(f'<td class="{band} entry-cell">{lc["entry_price"]:.2f}</td>')
                cells.append(f'<td class="{band} exit-cell">{lc["tgt_price"]:.2f}</td>')
                cells.append(f'<td class="{band}" style="{pnl_style(lc["points"])}">{lc["points"]:.2f}</td>')
                cells.append(f'<td class="{band}">{lc["invest"]:,.0f}</td>')
                cells.append(f'<td class="{band}" style="{pnl_style(lc["profit"])}">{lc["profit"]:,.0f}</td>')

            if i == 0:
                cells.append(f'<td rowspan="2" class="{band}" style="{pnl_style(net_profit_d)}">{net_invest_d:,.0f}</td>')
                cells.append(f'<td rowspan="2" class="{band}" style="{pnl_style(net_profit_d)}">{net_profit_d:,.0f}</td>')
                cells.append(f'<td rowspan="2" class="{band}" style="{pnl_style(net_profit_d)}">{net_pct_d:.1f}%</td>')
            d_body_rows.append('<tr>' + ''.join(cells) + '</tr>')

    decision_table_html = f"""
    <style>
        .decision-table-wrap {{ overflow: auto; max-height: 600px; border: 1px solid #d0d0d0; border-radius: 6px; margin-top: 8px; }}
        table.decision-table {{ border-collapse: collapse; width: 100%; font-size: 14px; white-space: nowrap; }}
        table.decision-table th, table.decision-table td {{
            border: 1px solid #d0d0d0; padding: 6px 10px; text-align: center;
        }}
        table.decision-table thead th {{
            background-color: #8ecae6; color: #1a1a1a; font-weight: 700;
            position: sticky; top: 0; z-index: 1;
        }}
        table.decision-table .row-band-a {{ background-color: #ffffff; }}
        table.decision-table .row-band-b {{ background-color: #f7f9fb; }}
        table.decision-table .sym-cell {{ background-color: #dbeeff !important; font-weight: 700; color: #0b3d91; }}
        table.decision-table .entry-cell {{ background-color: #c6efce; font-weight: 600; }}
        table.decision-table .exit-cell {{ background-color: #ffeb9c; font-weight: 600; }}
    </style>
    <div class="decision-table-wrap">
    <table class="decision-table">
    <thead>
    <tr>
        <th>STMBOL</th><th>ENTRY STRIKE</th><th>ENTRY PRICE</th><th>lot Size</th>
        <th>TGT STRIKE</th><th>TGT PRICE</th><th>Qty</th><th>entry</th><th>exit</th>
        <th>points</th><th>invest</th><th>profit</th>
        <th>Net Invest</th><th>Net Profit</th><th>profit%</th>
    </tr>
    </thead>
    <tbody>
    {''.join(d_body_rows)}
    </tbody>
    </table>
    </div>
    """
    st.markdown(decision_table_html, unsafe_allow_html=True)

    with st.expander("✏️ Edit / Delete Decision Row", expanded=False):
        d_options = {f"{r['symbol']} (row {r['id']})": r['id'] for r in decision_rows}
        d_choice = st.selectbox("Row", options=list(d_options.keys()), key="d_edit_choice")
        d_sel_id = d_options[d_choice]
        drow = next(r for r in decision_rows if r['id'] == d_sel_id)

        with st.form("edit_decision_form"):
            e_lot = st.number_input("lot Size", min_value=0, step=1, value=int(drow.get('lot_size') or 0))

            st.markdown("**CE leg**")
            ece1, ece2, ece3, ece4, ece5 = st.columns(5)
            e_ce_entry_strike = ece1.number_input("Entry Strike", min_value=0.0, step=0.5, format="%.1f", value=float(drow.get('ce_entry_strike') or 0.0))
            e_ce_entry_price = ece2.number_input("Entry Price", min_value=0.0, step=0.05, format="%.2f", value=float(drow.get('ce_entry_price') or 0.0))
            e_ce_tgt_strike = ece3.number_input("TGT Strike", min_value=0.0, step=0.5, format="%.1f", value=float(drow.get('ce_tgt_strike') or 0.0))
            e_ce_tgt_price = ece4.number_input("TGT Price", min_value=0.0, step=0.05, format="%.2f", value=float(drow.get('ce_tgt_price') or 0.0))
            e_ce_qty = ece5.number_input("Qty", min_value=1, step=1, value=int(drow.get('ce_qty') or 1))

            st.markdown("**PE leg**")
            epe1, epe2, epe3, epe4, epe5 = st.columns(5)
            e_pe_entry_strike = epe1.number_input("Entry Strike", min_value=0.0, step=0.5, format="%.1f", value=float(drow.get('pe_entry_strike') or 0.0))
            e_pe_entry_price = epe2.number_input("Entry Price", min_value=0.0, step=0.05, format="%.2f", value=float(drow.get('pe_entry_price') or 0.0))
            e_pe_tgt_strike = epe3.number_input("TGT Strike", min_value=0.0, step=0.5, format="%.1f", value=float(drow.get('pe_tgt_strike') or 0.0))
            e_pe_tgt_price = epe4.number_input("TGT Price", min_value=0.0, step=0.05, format="%.2f", value=float(drow.get('pe_tgt_price') or 0.0))
            e_pe_qty = epe5.number_input("Qty", min_value=1, step=1, value=int(drow.get('pe_qty') or 1))

            d_save_col, d_delete_col = st.columns(2)
            d_save_clicked = d_save_col.form_submit_button("💾 Save", use_container_width=True)
            d_delete_clicked = d_delete_col.form_submit_button("🗑️ Delete Row", use_container_width=True)

            if d_save_clicked:
                ce_taken = e_ce_entry_price > 0
                pe_taken = e_pe_entry_price > 0
                drow['lot_size'] = int(e_lot)
                drow['ce_entry_strike'] = e_ce_entry_strike if ce_taken else 0
                drow['ce_entry_price'] = e_ce_entry_price if ce_taken else 0
                drow['ce_tgt_strike'] = e_ce_tgt_strike if ce_taken else 0
                drow['ce_tgt_price'] = e_ce_tgt_price if ce_taken else 0
                drow['ce_qty'] = int(e_ce_qty) if ce_taken else 0
                drow['pe_entry_strike'] = e_pe_entry_strike if pe_taken else 0
                drow['pe_entry_price'] = e_pe_entry_price if pe_taken else 0
                drow['pe_tgt_strike'] = e_pe_tgt_strike if pe_taken else 0
                drow['pe_tgt_price'] = e_pe_tgt_price if pe_taken else 0
                drow['pe_qty'] = int(e_pe_qty) if pe_taken else 0
                save_decision_rows(decision_rows)
                st.success("Saved.")
                st.rerun()

            if d_delete_clicked:
                decision_rows = [r for r in decision_rows if r['id'] != d_sel_id]
                save_decision_rows(decision_rows)
                st.success("Deleted.")
                st.rerun()
else:
    st.info("No decision rows yet. Use **Add Decision Row** above.")
