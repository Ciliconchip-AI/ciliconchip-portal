"""Admin tab of the Ciliconchip Portal (Super Admin): who can sign in, their roles, and the audit log."""

from datetime import timedelta

import pandas as pd
import streamlit as st

from .db import temporary_password
from .portal_db import PORTAL_ROLES
from .ui_common import excel_bytes, flash, ist, section

ACTION_LABELS = {
    "login": "Signed in", "login_failed": "Sign-in failed", "logout": "Signed out", "auto_logout": "Signed out (idle)",
    "password_changed": "Changed own password", "user_added": "Added a sign-in", "roles_changed": "Changed roles",
    "user_deactivated": "Removed access", "user_reactivated": "Restored access", "password_reset": "Reset a password",
    "employee_added": "Added employee", "employee_updated": "Updated employee", "employees_imported": "Imported employees",
    "leave_applied": "Applied for leave", "leave_approved": "Approved leave", "leave_rejected": "Rejected leave",
    "leave_cancelled": "Cancelled leave", "leave_adjusted": "Adjusted leave balance",
    "leave_rules_changed": "Changed leave rules", "holidays_changed": "Changed holidays",
    "policy_published": "Published policy", "policy_retired": "Retired policy", "policy_opened": "Opened policy",
    "policy_acknowledged": "Acknowledged policy",
    "salary_saved": "Saved salary structure", "salaries_imported": "Imported salaries",
    "tax_choice_saved": "Saved tax choice", "opening_ytd_saved": "Saved opening year-to-date", "tax_lock_changed": "Locked/unlocked tax choices",
    "payroll_calculated": "Calculated payroll", "payroll_published": "Published payroll",
    "payroll_discarded": "Discarded payroll draft", "payslip_downloaded": "Downloaded payslip",
    "payslips_exported": "Exported all payslips", "finance_settings_changed": "Changed finance settings",
    "report_exported": "Exported report", "audit_exported": "Exported audit log",
    "resume_uploaded": "Uploaded resume", "duplicate_blocked": "Duplicate resume blocked",
    "profile_updated": "Updated candidate profile", "resume_deleted": "Deleted resume", "resume_viewed": "Opened resume",
    "resume_downloaded": "Downloaded resume", "resumes_downloaded": "Downloaded resumes (ZIP)",
    "list_exported": "Exported candidate list", "search": "Searched resumes", "job_matched": "Matched a job",
}


def render(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("adm_msg")
    if "adm_pw" in st.session_state:
        who, pw = st.session_state.pop("adm_pw")
        st.success(f"Temporary password for **{who}**: `{pw}`  \nShare it privately. It is shown only once; they "
                   f"choose their own password at first sign-in.")
    people = pdb.list_users()
    emps = {e["email"]: e for e in pdb.list_employees()}
    section("1.", "Who can sign in")
    st.dataframe(pd.DataFrame([{
        "Name": u["full_name"], "Email": u["email"],
        "Roles": ", ".join(pdb.roles_of(u)) or "Employee only",
        "Employee record": "Yes" if u["email"] in emps else "No",
        "Access": "Active" if u["active"] else "Removed",
        "Password": "Temporary – not changed yet" if u["must_change_password"] else "Set by user",
        "Last sign-in (IST)": ist(u["last_login_at"]) or "Never", "Added by": u["created_by"] or "",
    } for u in people]), hide_index=True, width="stretch")
    st.caption("Everyone with a sign-in is an employee (Resource): own leave, policies, payslips. Managers are "
               "recognised automatically from the hardline-manager field. Extra roles: "
               + " · ".join(f"**{k}** – {v}" for k, v in PORTAL_ROLES.items()))

    left, right = st.columns(2)
    with left:
        section("2.", "Change roles or access")
        choices = {f"{u['full_name']} <{u['email']}>": u for u in people}
        pick = st.selectbox("Person", list(choices), key="adm_person")
        target = choices[pick]
        with st.form(f"roles_{target['email']}"):
            roles = st.multiselect("Roles", list(PORTAL_ROLES), default=pdb.roles_of(target))
            action = st.radio("Also", ["Nothing else", "Remove access", "Restore access", "Reset password"],
                              horizontal=True)
            if st.form_submit_button("Apply", width="stretch"):
                is_me = target["email"] == me["email"]
                losing_super = "Super Admin" in pdb.roles_of(target) and (
                    "Super Admin" not in roles or action == "Remove access")
                if losing_super and target["active"] and pdb.user_count(super_admins_only=True) <= 1:
                    st.error("This is the only active Super Admin. Give the role to someone else first.")
                elif is_me and action == "Remove access":
                    st.error("You cannot remove your own access.")
                else:
                    old = pdb.roles_of(target)
                    if sorted(old) != sorted(roles):
                        pdb.update_user(target["email"], roles=",".join(roles))
                        ctx.audit("roles_changed", f"{target['full_name']}: {', '.join(old) or 'none'} -> "
                                                   f"{', '.join(roles) or 'none'}", target["email"])
                    if action == "Remove access" and target["active"]:
                        pdb.update_user(target["email"], active=0)
                        ctx.audit("user_deactivated", target["full_name"], target["email"])
                    elif action == "Restore access" and not target["active"]:
                        pdb.update_user(target["email"], active=1, failed_logins=0, locked_until=None)
                        ctx.audit("user_reactivated", target["full_name"], target["email"])
                    elif action == "Reset password":
                        pw = temporary_password()
                        pdb.set_password(target["email"], pw, must_change=True)
                        ctx.audit("password_reset", target["full_name"], target["email"])
                        st.session_state["adm_pw"] = (target["full_name"], pw)
                    st.session_state["adm_msg"] = f"Saved for {target['full_name']}."
                    st.rerun()
    with right:
        section("3.", "Add a sign-in without an employee record")
        st.caption("Employees normally get their sign-in when HR adds them. Use this only for people who are not "
                   "on the payroll, e.g. an external recruiter who needs the Resume Bank.")
        with st.form("adm_add", clear_on_submit=True):
            n = st.text_input("Full name")
            em = st.text_input("Email")
            rl = st.multiselect("Roles", list(PORTAL_ROLES), default=["TA Viewer"])
            if st.form_submit_button("Add Sign-in", width="stretch"):
                pw = temporary_password()
                err = pdb.create_user(em, n, rl, pw, created_by=me["email"])
                if err:
                    st.error(err)
                else:
                    ctx.audit("user_added", f"{n.strip()} <{em.strip().lower()}> roles {', '.join(rl) or 'none'}",
                              em.strip().lower())
                    st.session_state["adm_pw"] = (n.strip(), pw)
                    st.rerun()

    section("4.", "Audit log – who did what")
    f1, f2, f3, f4 = st.columns([1.3, 1.6, 1, 1])
    emails = {"Everyone": ""} | {f"{u['full_name']} <{u['email']}>": u["email"] for u in people}
    who = f1.selectbox("Person", list(emails), key="aud_who")
    acts = f2.multiselect("Actions", list(ACTION_LABELS), format_func=ACTION_LABELS.get, key="aud_acts",
                          placeholder="All actions")
    t = (pd.Timestamp.now("UTC").tz_localize(None) + timedelta(hours=5, minutes=30)).date()
    d_from = f3.date_input("From", t - timedelta(days=30), key="aud_from", format="DD/MM/YYYY")
    d_to = f4.date_input("To", t, key="aud_to", format="DD/MM/YYYY")
    to_utc = lambda x: (pd.Timestamp(x) - timedelta(hours=5, minutes=30)).to_pydatetime()
    log = pdb.audit_log(emails[who], acts or None, to_utc(d_from), to_utc(d_to + timedelta(days=1)))
    if not log:
        st.caption("No events for these filters.")
        return
    lt = pd.DataFrame([{"When (IST)": ist(e["at"], "%d %b %Y, %H:%M:%S"), "Person": e["user_name"] or "",
                        "Email": e["user_email"] or "", "Action": ACTION_LABELS.get(e["action"], e["action"]),
                        "Details": e["details"] or ""} for e in log])
    st.dataframe(lt, hide_index=True, width="stretch", height=380)
    st.caption(f"{len(log)} event(s). Events cannot be edited or deleted from the app. Salary amounts are never "
               f"written to the log.")
    st.download_button("Download audit log (Excel)", excel_bytes({"Audit log": lt}),
                       f"portal_audit_{d_from:%Y%m%d}_{d_to:%Y%m%d}.xlsx", on_click=ctx.audit,
                       args=("audit_exported", f"{len(log)} event(s)"))
