"""Small helpers shared by the Portal screens."""

import io
from datetime import timedelta

import pandas as pd
import streamlit as st

IST = timedelta(hours=5, minutes=30)


def ist(dt, fmt="%d %b %Y, %H:%M"):
    return (dt + IST).strftime(fmt) if dt else ""


def d(dt, fmt="%d %b %Y"):
    return dt.strftime(fmt) if dt else ""


def section(num, title):
    st.markdown(f'<div class="section-label"><span>{num}</span> {title}</div>', unsafe_allow_html=True)


def card(value, label, color=None):
    style = f' style="color:{color}"' if color else ""
    st.markdown(f'<div class="metric-card"><div class="metric-value"{style}>{value}</div>'
                f'<div class="metric-label">{label}</div></div>', unsafe_allow_html=True)


def excel_bytes(frames: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in frames.items():
            df.to_excel(xw, index=False, sheet_name=name[:31])
    return buf.getvalue()


def inr(v) -> str:
    v = int(round(v or 0))
    s = str(abs(v))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if v < 0 else "") + "₹" + s


def flash(key, kind="success"):
    """Show a message stored before st.rerun()."""
    msg = st.session_state.pop(key, None)
    if msg:
        getattr(st, kind)(msg)
