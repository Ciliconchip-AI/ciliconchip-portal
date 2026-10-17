"""
Ciliconchip Portal - HR, Finance and the Resume Bank in one app (Powered by AI COE).
"""

import base64
import hmac
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import streamlit as st
from cryptography.fernet import Fernet

from src import ui_admin, ui_finance, ui_hr, ui_resume_bank
from src.ai import load_embedder, profile_text, to_blob
from src.db import ResumeDB, password_problem
from src.portal_db import PortalDB, today_ist
from src.security import Vault
from src.ui_common import card, ist, section

APP_VERSION = "Portal v1.0 (2026-10-17)"
IDLE_MINUTES = 30

st.set_page_config(
    page_title="Ciliconchip Portal - Powered by AI COE",
    page_icon="🟠",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    :root { --orange:#FF6600; --ink:#17181C; --muted:#666B73; --line:#E7E8EC; --soft:#F7F7F9; }
    :root, html, body, .stApp { color-scheme: light !important; }
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] { background:#fff !important; }
    .block-container { padding-top:1.2rem !important; padding-bottom:2rem !important; max-width:1400px !important; }

    .ats-header { display:flex; align-items:center; gap:26px; width:100%; margin:24px 0 26px 0;
                  padding:0 8px; box-sizing:border-box; }
    .ats-logo-wrap { flex:0 0 235px; display:flex; align-items:center; justify-content:center; }
    .ats-logo { width:235px; max-width:100%; height:auto; display:block; }
    .ats-header-divider { width:1px; height:92px; background:var(--line); flex:0 0 1px; }
    .ats-title-container { flex:1 1 auto; min-width:0; }
    .ats-title { font-family:Arial, Helvetica, sans-serif !important; font-size:clamp(32px,3vw,46px) !important;
                 font-weight:800 !important; line-height:1.14 !important; letter-spacing:-1px !important;
                 color:var(--ink) !important; margin:0 !important; }
    .ats-powered { display:inline-block; margin-top:4px; font-size:0.62em; font-weight:700; letter-spacing:-0.4px; }
    .ats-powered .sep { color:var(--orange); font-weight:800; margin-right:6px; }
    .ats-powered .txt { color:var(--orange); }
    .ats-subtitle { color:var(--muted); font-size:17px; line-height:1.45; margin:10px 0 0; }

    .section-label { font-size:18px; font-weight:750; color:var(--ink); margin:8px 0 10px; }
    .section-label span { color:var(--orange); }
    .metric-card { border:1px solid var(--line); border-radius:12px; padding:14px; background:var(--soft); }
    .metric-value { font-size:28px; font-weight:800; }
    .metric-label { color:var(--muted); font-size:12px; }
    .pill { display:inline-block; background:#FFF1E9; color:#B84800; border-radius:999px; padding:4px 9px;
            margin:2px 4px 2px 0; font-size:12px; }
    .pill.sec { background:#F1F2F5; color:#454A52; }

    .stButton > button, .stFormSubmitButton > button { background:var(--orange); color:#fff; border:0;
            border-radius:8px; font-weight:750; min-height:44px; }
    .stButton > button:hover, .stFormSubmitButton > button:hover { background:#E95700; color:#fff; }
    .stDownloadButton > button { border:1px solid var(--orange); color:var(--orange); background:#fff;
            border-radius:8px; font-weight:700; }
    [data-testid="stFileUploader"] { border:1.5px dashed var(--orange); border-radius:12px; padding:8px;
            background:#FFFDFC; }
    .stTabs [data-baseweb="tab"] { font-size:16px; font-weight:700; }
    .stTabs [aria-selected="true"] { color:var(--orange) !important; }
    .stTabs [data-baseweb="tab-highlight"] { background-color:var(--orange) !important; }

    .footer { color:#8A8F98; text-align:center; font-size:11px; margin-top:35px; padding-top:15px;
              border-top:1px solid var(--line); }

    @media (max-width: 900px) {
        .ats-header { gap:16px; }
        .ats-logo-wrap { flex-basis:150px; }
        .ats-logo { width:150px; }
        .ats-header-divider { height:70px; }
        .ats-title { font-size:28px !important; }
        .ats-subtitle { font-size:14px; }
    }
    @media (max-width: 640px) {
        .ats-header { flex-direction:column; align-items:flex-start; gap:10px; margin:48px 0 18px 0; }
        .ats-logo-wrap { flex-basis:auto; justify-content:flex-start; }
        .ats-header-divider { display:none; }
        .ats-title { font-size:30px !important; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "ciliconchip_logo.png")
if os.path.exists(LOGO):
    with open(LOGO, "rb") as fh:
        logo_html = (f'<img class="ats-logo" alt="Ciliconchip logo" '
                     f'src="data:image/png;base64,{base64.b64encode(fh.read()).decode()}">')
else:
    logo_html = '<div style="color:#FF6600;font-weight:800;font-size:28px;">Ciliconchip</div>'

st.markdown(
    f"""<div class="ats-header">
<div class="ats-logo-wrap">{logo_html}</div>
<div class="ats-header-divider"></div>
<div class="ats-title-container">
<div class="ats-title">Ciliconchip Portal<br>
<span class="ats-powered"><span class="sep">–</span><span class="txt">Powered by AI COE</span></span></div>
<div class="ats-subtitle">HR, Finance and Talent Acquisition in one secure place.</div>
</div>
</div>""",
    unsafe_allow_html=True,
)


def footer():
    st.markdown(
        f'<div class="footer">Ciliconchip Portal – Powered by AI COE | Employee and candidate data is confidential '
        f'and for internal use only. &nbsp;|&nbsp; {APP_VERSION}</div>',
        unsafe_allow_html=True,
    )


def secret(name):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


APP_PASSWORD = secret("APP_PASSWORD") or os.environ.get("APP_PASSWORD")
ENC_KEY = secret("PORTAL_ENCRYPTION_KEY") or os.environ.get("PORTAL_ENCRYPTION_KEY")


@st.cache_resource(show_spinner="Connecting to the database...")
def connect(cfg_items, key):
    rdb = ResumeDB(dict(cfg_items) if cfg_items else None)
    if not key and not cfg_items:
        key = Fernet.generate_key().decode()   # local test mode only: a throw-away key
    return rdb, PortalDB(rdb.engine, Vault(key))


mysql_cfg = secret("mysql")
cfg_items = tuple(sorted(dict(mysql_cfg).items())) if mysql_cfg else None
try:
    rdb, pdb = connect(cfg_items, ENC_KEY)
except Exception as exc:
    st.error("Could not connect to the MySQL database. Please check the connection details in the app's secrets, "
             "and that the database service is running (Aiven's free plan switches off when idle).")
    st.caption(f"Technical detail: {str(exc)[:300]}")
    footer()
    st.stop()


pdb.new_run()   # fresh page cache for this click


def audit(action, details="", ref=""):
    pdb.audit(st.session_state.get("user"), action, details, ref)


def centred():
    return st.columns([1, 1.3, 1])[1]


# -------------------------------------------------------------------
# Sign-in
# -------------------------------------------------------------------
user = st.session_state.get("user")
if user:
    idle = time.time() - st.session_state.get("last_seen", time.time())
    fresh = pdb.get_user(user["email"])
    if idle > IDLE_MINUTES * 60:
        audit("auto_logout", f"after {IDLE_MINUTES} minutes without activity")
        st.session_state.clear()
        st.session_state["signin_note"] = f"You were signed out after {IDLE_MINUTES} minutes without activity."
        user = None
    elif not fresh or not fresh["active"]:
        st.session_state.clear()
        st.session_state["signin_note"] = "Your access to the Portal has been removed. Please contact the admin."
        user = None
    else:
        user = {"email": fresh["email"], "full_name": fresh["full_name"], "roles": pdb.roles_of(fresh),
                "must_change_password": fresh["must_change_password"]}
        st.session_state["user"] = user
st.session_state["last_seen"] = time.time()

if pdb.user_count() == 0:
    with centred():
        st.markdown('<div class="section-label"><span>●</span> First-time setup – create the admin account</div>',
                    unsafe_allow_html=True)
        st.caption("Nobody has a sign-in yet. Create the first Super Admin; they then add everyone else."
                   + (" Enter the setup password from the app's secrets (APP_PASSWORD) to prove you may do this."
                      if APP_PASSWORD else ""))
        with st.form("setup"):
            team_pw = st.text_input("Setup password", type="password") if APP_PASSWORD else ""
            name = st.text_input("Your full name")
            email = st.text_input("Your work email")
            pw1 = st.text_input("Choose a password", type="password", help="At least 8 characters, letters and numbers.")
            pw2 = st.text_input("Type it again", type="password")
            extra = st.multiselect("Also give me these roles", ["HR Admin", "Finance Admin", "Recruiter"],
                                   default=["HR Admin", "Finance Admin", "Recruiter"])
            if st.form_submit_button("Create Admin Account", width="stretch"):
                if APP_PASSWORD and not hmac.compare_digest(team_pw.encode(), str(APP_PASSWORD).encode()):
                    st.error("The setup password is not correct.")
                elif pw1 != pw2:
                    st.error("The two passwords do not match.")
                elif password_problem(pw1):
                    st.error(password_problem(pw1))
                else:
                    err = pdb.create_user(email, name, ["Super Admin"] + extra, pw1, created_by="first-time setup",
                                          must_change=False)
                    if err:
                        st.error(err)
                    else:
                        st.session_state["user"] = {"email": email.strip().lower(), "full_name": name.strip(),
                                                    "roles": ["Super Admin"] + extra, "must_change_password": 0}
                        audit("user_added", f"{name.strip()} <{email.strip().lower()}> as Super Admin "
                                            f"(first-time setup)")
                        audit("login")
                        st.rerun()
    footer()
    st.stop()

if not user:
    with centred():
        st.markdown('<div class="section-label"><span>●</span> Sign in</div>', unsafe_allow_html=True)
        st.caption("Sign in with your work email and the password given to you.")
        if st.session_state.get("signin_note"):
            st.warning(st.session_state["signin_note"])
        with st.form("login"):
            email = st.text_input("Work email")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", width="stretch"):
                found, msg = pdb.verify_login(email, pw)
                if found:
                    st.session_state.pop("signin_note", None)
                    st.session_state["user"] = {"email": found["email"], "full_name": found["full_name"],
                                                "roles": pdb.roles_of(found),
                                                "must_change_password": found["must_change_password"]}
                    audit("login")
                    st.rerun()
                pdb.audit({"email": email.strip().lower()}, "login_failed", msg)
                st.error(msg)
        st.caption("Forgot your password? Ask HR or the Portal admin to reset it.")
    footer()
    st.stop()

if user["must_change_password"]:
    with centred():
        st.markdown('<div class="section-label"><span>●</span> Choose your own password</div>',
                    unsafe_allow_html=True)
        st.caption(f"Welcome, {user['full_name']}. You signed in with a temporary password; please choose your own.")
        with st.form("new_password"):
            pw1 = st.text_input("New password", type="password", help="At least 8 characters, letters and numbers.")
            pw2 = st.text_input("Type it again", type="password")
            if st.form_submit_button("Save Password", width="stretch"):
                if pw1 != pw2:
                    st.error("The two passwords do not match.")
                elif password_problem(pw1):
                    st.error(password_problem(pw1))
                else:
                    pdb.set_password(user["email"], pw1, must_change=False)
                    audit("password_changed", "Replaced temporary password")
                    st.rerun()
    footer()
    st.stop()

# -------------------------------------------------------------------
# Signed in
# -------------------------------------------------------------------
roles = set(user["roles"])
ctx = SimpleNamespace(
    pdb=pdb, user=user, audit=audit, emp=pdb.get_employee(user["email"]),
    is_super="Super Admin" in roles, is_hr="HR Admin" in roles, is_fin="Finance Admin" in roles,
    is_manager=pdb.is_manager(user["email"]), vault_ready=pdb.vault.ready, settings=pdb.settings(),
)
rb_access = bool(roles & {"Super Admin", "Recruiter", "TA Viewer"})

if not cfg_items:
    st.warning("Running on temporary local storage (no MySQL connection configured) with a throw-away encryption "
               "key. Everything is lost when the app restarts. Add the [mysql] section and PORTAL_ENCRYPTION_KEY "
               "to the app's secrets.")
elif not pdb.vault.ready and (ctx.is_super or ctx.is_fin or ctx.is_hr):
    st.warning("PORTAL_ENCRYPTION_KEY is missing or invalid in the app's secrets, so Finance and the PAN / bank "
               "fields are switched off.")
    if ctx.is_super:
        st.session_state.setdefault("suggested_key", Fernet.generate_key().decode())
        st.markdown("Add this line to the app's **Settings → Secrets** on Streamlit (above the `[mysql]` line), "
                    "save, and keep a copy of the key somewhere safe and offline – without it, salary data cannot "
                    "be read again:")
        st.code(f'PORTAL_ENCRYPTION_KEY = "{st.session_state["suggested_key"]}"', language="toml")

top_l, top_r = st.columns([5, 1])
with top_l:
    unread = pdb.notifications(user["email"], unread_only=True)
    if unread:
        st.info(f"🔔 You have {len(unread)} new notification(s) – see Home.")
with top_r:
    tag = ", ".join(sorted(roles) + (["Manager"] if ctx.is_manager else [])) or "Employee"
    st.markdown(f'<div style="text-align:right;font-size:13px;line-height:1.4">Signed in as<br><b>{user["full_name"]}'
                f'</b><br><span style="color:#666">{tag}</span></div>', unsafe_allow_html=True)
    with st.popover("My account", width="stretch"):
        with st.form("change_pw", clear_on_submit=True):
            st.markdown("**Change my password**")
            cur_pw = st.text_input("Current password", type="password")
            pw1 = st.text_input("New password", type="password")
            pw2 = st.text_input("Type it again", type="password")
            if st.form_submit_button("Change Password", width="stretch"):
                ok, _ = pdb.verify_login(user["email"], cur_pw)
                if not ok:
                    st.error("Current password is not correct.")
                elif pw1 != pw2:
                    st.error("The two new passwords do not match.")
                elif password_problem(pw1):
                    st.error(password_problem(pw1))
                else:
                    pdb.set_password(user["email"], pw1, must_change=False)
                    audit("password_changed")
                    st.success("Password changed.")
        if st.button("Sign out", width="stretch"):
            audit("logout")
            st.session_state.clear()
            st.rerun()

tab_names = ["Home", "HR", "Finance"] + (["Resume Bank"] if rb_access else []) + (["Admin"] if ctx.is_super else [])
tabs = dict(zip(tab_names, st.tabs(tab_names)))

# ===================================================================
# HOME
# ===================================================================
with tabs["Home"]:
    hello = user["full_name"].split()[0] if user["full_name"] else ""
    st.markdown(f"### Hello {hello}")
    notes = pdb.notifications(user["email"], limit=10)
    c1, c2 = st.columns([3, 2])
    with c1:
        section("1.", "Notifications")
        if notes:
            for n in notes:
                st.markdown(("🟠 " if not n["read_at"] else "▫️ ") + f"{n['message']}  \n"
                            f"<span style='color:#8A8F98;font-size:12px'>{ist(n['at'])}</span>",
                            unsafe_allow_html=True)
            if unread and st.button("Mark all as read"):
                pdb.mark_read(user["email"])
                st.rerun()
        else:
            st.caption("No notifications.")
    with c2:
        section("2.", "At a glance")
        if ctx.emp:
            bal = pdb.balance_for(user["email"])
            paid = [t for t in pdb.leave_types() if t.paid]
            cols = st.columns(len(paid) or 1)
            for col, t in zip(cols, paid):
                with col:
                    card(f"{(bal.get(t.code) or {}).get('available', 0):g}", f"{t.code} AVAILABLE")
            if ctx.is_manager:
                waiting = pdb.leave_requests(approver=user["email"], statuses=["Pending"])
                st.markdown(f"**{len(waiting)}** leave request(s) waiting for your approval (HR > Approvals).")
            pols = pdb.list_policies()
            acked = {a["policy_id"] for a in pdb.acks(email=user["email"])}
            todo = [p for p in pols if p["id"] not in acked]
            if todo:
                st.markdown(f"**{len(todo)}** policy(ies) to read and acknowledge (HR > Policies).")
            nxt = [h for h in pdb.holiday_list(today_ist().year) + pdb.holiday_list(today_ist().year + 1)
                   if h["day"] >= today_ist()][:3]
            if nxt:
                st.markdown("**Next holidays:** " + ", ".join(f"{h['name']} ({h['day']:%d %b})" for h in nxt))
            if pdb.vault.ready:
                slips = pdb.my_payslips(user["email"])
                if slips:
                    st.markdown(f"**Latest payslip:** {datetime.strptime(slips[0]['month'], '%Y-%m'):%B %Y} "
                                f"(Finance > My Payslips)")
        else:
            st.caption("Your employee record is not set up yet, so leave and payslips are not available. "
                       "HR adds employees under HR > Employees.")

with tabs["HR"]:
    ui_hr.render(ctx)

with tabs["Finance"]:
    ui_finance.render(ctx)

if rb_access:
    with tabs["Resume Bank"]:
        @st.cache_resource(show_spinner="Loading the AI model (the first start after an update takes about a minute)...")
        def get_ai():
            try:
                return load_embedder(), None
            except Exception as exc:
                return None, str(exc)

        ai, ai_error = get_ai()
        if ai is not None:
            missing = rdb.needs_embedding(ai.name)
            if missing:
                with st.spinner(f"Building AI fingerprints for {len(missing)} saved resume(s)... (one-time)"):
                    for i in range(0, len(missing), 32):
                        batch = missing[i:i + 32]
                        vecs = ai.encode([profile_text(r) for r in batch])
                        rdb.set_embeddings([(r["id"], to_blob(v)) for r, v in zip(batch, vecs)], ai.name)
        rb_user = {"email": user["email"], "full_name": user["full_name"]}
        ui_resume_bank.render(rdb, ai, ai_error, rb_user, audit, IS_ADMIN=ctx.is_super,
                              CAN_EDIT=bool(roles & {"Super Admin", "Recruiter"}))

if ctx.is_super:
    with tabs["Admin"]:
        ui_admin.render(ctx)

footer()
