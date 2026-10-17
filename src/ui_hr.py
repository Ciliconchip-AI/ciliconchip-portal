"""HR tab of the Ciliconchip Portal: self-service for every employee, approvals for managers, admin for HR."""

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from .portal_db import PORTAL_ROLES, today_ist
from .db import temporary_password
from .security import mask
from .ui_common import card, d, excel_bytes, flash, ist, section

HALF = ["Full day", "First half", "Second half"]
STATES = ["", "Karnataka", "Tamil Nadu", "Telangana", "Andhra Pradesh", "Maharashtra", "Kerala", "Delhi",
          "Haryana", "Uttar Pradesh", "West Bengal", "Gujarat", "Rajasthan", "Punjab", "Madhya Pradesh", "Odisha",
          "Other"]


def render(ctx):
    pdb, me = ctx.pdb, ctx.user
    names = ["My Leave", "Apply Leave", "Policies", "Holidays"]
    if ctx.is_manager or ctx.is_hr:
        names += ["Approvals", "Team"]
    if ctx.is_hr:
        names += ["Employees", "Leave Admin", "Policy Admin", "Leave Rules & Holidays"]
    tabs = dict(zip(names, st.tabs(names)))
    with tabs["My Leave"]:
        my_leave(ctx)
    with tabs["Apply Leave"]:
        apply_leave(ctx)
    with tabs["Policies"]:
        policies(ctx)
    with tabs["Holidays"]:
        holidays_view(ctx)
    if "Approvals" in tabs:
        with tabs["Approvals"]:
            approvals(ctx)
        with tabs["Team"]:
            team(ctx)
    if ctx.is_hr:
        with tabs["Employees"]:
            employees_admin(ctx)
        with tabs["Leave Admin"]:
            leave_admin(ctx)
        with tabs["Policy Admin"]:
            policy_admin(ctx)
        with tabs["Leave Rules & Holidays"]:
            rules_admin(ctx)


def _need_employee(ctx):
    if not ctx.emp:
        st.info("Your employee record has not been set up yet, so leave is not available. Please ask HR to add "
                "you under HR > Employees (using the same email you sign in with).")
        return False
    return True


# ===================================================================== self-service
def balance_table(pdb, email, as_of=None):
    bal = pdb.balance_for(email, as_of)
    types = pdb.leave_types()
    rows = []
    for t in types:
        b = bal.get(t.code) or {}
        rows.append({"Leave": f"{t.name} ({t.code})",
                     "Carried forward": "" if not t.paid else f"{b.get('opening', 0):g}",
                     "Credited this year": "" if not t.paid else f"{b.get('accrued', 0):g}",
                     "HR adjustments": "" if not t.paid else f"{b.get('adjusted', 0):g}",
                     "Taken": f"{b.get('used', 0):g}", "Pending approval": f"{b.get('pending', 0):g}",
                     "Available now": "No limit (unpaid)" if not t.paid else f"{b.get('available', 0):g}"})
    return bal, types, pd.DataFrame(rows)


def my_leave(ctx):
    pdb, me = ctx.pdb, ctx.user
    if not _need_employee(ctx):
        return
    flash("leave_msg")
    bal, types, df = balance_table(pdb, me["email"])
    section("1.", f"My leave balance – {today_ist().year}")
    cols = st.columns(len([t for t in types if t.paid]) or 1)
    for col, t in zip(cols, [t for t in types if t.paid]):
        with col:
            card(f"{(bal.get(t.code) or {}).get('available', 0):g}", f"{t.name.upper()} AVAILABLE")
    st.dataframe(df, hide_index=True, width="stretch")
    st.caption(f"Leave is credited on the 1st of every month from your joining date "
               f"({d(ctx.emp['date_of_joining'])}). "
               + "; ".join(f"{t.code}: {t.monthly_accrual:g}/month" + (f", carries forward up to {t.carry_forward_cap:g}"
                                                                     if not t.lapse_year_end else ", lapses on 31 Dec")
                           for t in types if t.paid) + ".")

    section("2.", "My leave requests")
    reqs = pdb.leave_requests(email=me["email"])
    if not reqs:
        st.caption("No leave requests yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Type": r["leave_type"], "From": d(r["start_date"]), "To": d(r["end_date"]),
        "Days": f"{r['days']:g}" + (f" ({r['half_day']})" if r["half_day"] else ""), "Status": r["status"],
        "Reason": r["reason"] or "", "Applied on": ist(r["applied_at"]),
        "Decided by": r["decided_by"] or "", "Comment": r["decision_comment"] or "",
    } for r in reqs]), hide_index=True, width="stretch")
    cancellable = [r for r in reqs if r["status"] == "Pending"
                   or (r["status"] == "Approved" and r["start_date"] > today_ist())]
    if cancellable:
        with st.form("cancel_leave"):
            labels = {f"{r['leave_type']} {d(r['start_date'])} – {d(r['end_date'])} ({r['status']})": r
                      for r in cancellable}
            pick = st.selectbox("Cancel a request", list(labels))
            if st.form_submit_button("Cancel Request"):
                r = labels[pick]
                err = pdb.cancel_leave(r["id"], me["email"], as_hr=False)
                if err:
                    st.error(err)
                else:
                    ctx.audit("leave_cancelled", f"{r['leave_type']} {r['start_date']} to {r['end_date']}", r["id"])
                    st.session_state["leave_msg"] = "Request cancelled; the days are back in your balance."
                    st.rerun()


def apply_leave(ctx):
    pdb, me = ctx.pdb, ctx.user
    if not _need_employee(ctx):
        return
    flash("apply_msg")
    types = pdb.leave_types()
    bal = pdb.balance_for(me["email"])
    section("1.", "Apply for leave")
    mgr = pdb.get_employee(ctx.emp["manager_email"]) if ctx.emp["manager_email"] else None
    st.caption("Goes to your manager for approval: " + (f"**{mgr['full_name']}**" if mgr else
                                                        "**HR** (no manager is set for you)"))
    opts = {f"{t.name} ({t.code})" + ("" if not t.paid else f" – {(bal.get(t.code) or {}).get('available', 0):g} "
                                                            f"available"): t for t in types}
    with st.form("apply_leave", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            pick = st.selectbox("Leave type", list(opts))
        with c2:
            start = st.date_input("From", today_ist(), format="DD/MM/YYYY")
        with c3:
            end = st.date_input("To", today_ist(), format="DD/MM/YYYY")
        half = st.radio("Duration (for a single day)", HALF, horizontal=True)
        reason = st.text_area("Reason", max_chars=500, placeholder="e.g. Family function in Chennai")
        if st.form_submit_button("Submit Leave Request", width="stretch"):
            if not reason.strip():
                st.error("Please give a reason.")
            else:
                t = opts[pick]
                res = pdb.apply_leave(me["email"], t.code, start, end, "" if half == "Full day" else half,
                                      reason.strip())
                if res.startswith("ok:"):
                    ctx.audit("leave_applied", f"{t.code} {start} to {end}", res[3:])
                    st.session_state["apply_msg"] = (f"Leave request submitted ({t.name}, {d(start)} – {d(end)}). "
                                                     f"It is now waiting for approval.")
                    st.rerun()
                else:
                    st.error(res)
    st.caption("Saturdays, Sundays and company holidays are not counted. Leave Without Pay reduces salary for "
               "those days.")


def policies(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("pol_msg")
    section("1.", "Company policies")
    pols = pdb.list_policies()
    if not pols:
        st.caption("No policies published yet.")
        return
    acked = {a["policy_id"] for a in pdb.acks(email=me["email"])}
    for p in pols:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(f"**{p['title']}**  ·  {p['category'] or 'General'}  ·  version {p['version']}"
                            + (f"  ·  effective {d(p['effective_date'])}" if p["effective_date"] else ""))
                if p["summary"]:
                    st.caption(p["summary"])
                st.caption("✓ You have acknowledged this policy." if p["id"] in acked else
                           "Please read and acknowledge this policy.")
            with c2:
                f = pdb.policy_file(p["id"])
                if f and f[1]:
                    st.download_button("Open / download", f[1], f[0], key=f"pol_{p['id']}", width="stretch",
                                       on_click=ctx.audit, args=("policy_opened", p["title"], p["id"]))
                if p["id"] not in acked and st.button("I have read this", key=f"ack_{p['id']}", width="stretch"):
                    pdb.acknowledge(p["id"], me["email"])
                    ctx.audit("policy_acknowledged", p["title"], p["id"])
                    st.session_state["pol_msg"] = f"Thank you – acknowledgement of '{p['title']}' recorded."
                    st.rerun()


def holidays_view(ctx):
    year = st.selectbox("Year", [today_ist().year, today_ist().year + 1], key="hol_year")
    hol = ctx.pdb.holiday_list(year)
    section("1.", f"Holidays {year}")
    if hol:
        st.dataframe(pd.DataFrame([{"Date": d(h["day"]), "Day": h["day"].strftime("%A"), "Holiday": h["name"]}
                                   for h in hol]), hide_index=True, width="stretch")
    else:
        st.caption("No holidays published for this year yet.")


# ===================================================================== managers
def approvals(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("appr_msg")
    pending = pdb.leave_requests(approver=me["email"], statuses=["Pending"]) if ctx.is_manager else []
    if ctx.is_hr:
        everything = pdb.leave_requests(statuses=["Pending"])
        mine = {r["id"] for r in pending}
        pending += [r for r in everything if r["id"] not in mine and r["employee_email"] != me["email"]]
    section("1.", f"Leave requests waiting for a decision ({len(pending)})")
    if ctx.is_hr:
        st.caption("As HR you see every pending request, so you can step in when a manager is away or no "
                   "manager is set.")
    if not pending:
        st.caption("Nothing waiting.")
        return
    people = {e["email"]: e for e in pdb.list_employees()}
    for r in sorted(pending, key=lambda x: x["start_date"]):
        e = people.get(r["employee_email"], {})
        bal = pdb.balance_for(r["employee_email"]).get(r["leave_type"]) or {}
        with st.form(f"decide_{r['id']}"):
            st.markdown(f"**{e.get('full_name', r['employee_email'])}** · {r['leave_type']} · "
                        f"{d(r['start_date'])} – {d(r['end_date'])} · **{r['days']:g} day(s)**"
                        + (f" ({r['half_day']})" if r["half_day"] else ""))
            st.caption(f"Reason: {r['reason'] or '—'}  ·  Applied {ist(r['applied_at'])}  ·  "
                       + ("Balance after this request: " + f"{bal.get('available'):g}"
                          if bal.get("available") is not None else "Unpaid leave")
                       + ("" if r["approver_email"] == me["email"] else
                          f"  ·  Manager: {r['approver_email'] or 'not set'}"))
            comment = st.text_input("Comment (optional)", key=f"cm_{r['id']}")
            c1, c2, _ = st.columns([1, 1, 3])
            ok = c1.form_submit_button("Approve", width="stretch")
            no = c2.form_submit_button("Reject", width="stretch")
            if ok or no:
                decision = "Approved" if ok else "Rejected"
                err = pdb.decide_leave(r["id"], decision, me["email"], comment.strip(),
                                       as_hr=ctx.is_hr and r["approver_email"] != me["email"])
                if err:
                    st.error(err)
                else:
                    ctx.audit("leave_" + decision.lower(), f"{e.get('full_name')} {r['leave_type']} "
                                                           f"{r['start_date']} to {r['end_date']}", r["id"])
                    st.session_state["appr_msg"] = f"{decision}: {e.get('full_name')}'s {r['leave_type']} request."
                    st.rerun()


def team(ctx):
    pdb, me = ctx.pdb, ctx.user
    members = pdb.list_employees(include_exited=False) if ctx.is_hr else pdb.team_of(me["email"])
    if ctx.is_hr and ctx.is_manager:
        scope = st.radio("Show", ["My team", "Everyone"], horizontal=True, key="team_scope")
        if scope == "My team":
            members = pdb.team_of(me["email"])
    section("1.", "Who is on leave – next 30 days")
    emails = {m["email"] for m in members}
    t0 = today_ist()
    upcoming = [r for r in pdb.leave_requests(statuses=["Approved", "Pending"], since=t0, until=t0 + timedelta(days=30))
                if r["employee_email"] in emails]
    names = {m["email"]: m["full_name"] for m in members}
    if upcoming:
        st.dataframe(pd.DataFrame([{"Name": names.get(r["employee_email"]), "Type": r["leave_type"],
                                    "From": d(r["start_date"]), "To": d(r["end_date"]), "Days": r["days"],
                                    "Status": r["status"]} for r in sorted(upcoming, key=lambda x: x["start_date"])]),
                     hide_index=True, width="stretch")
    else:
        st.caption("Nobody in this group has leave booked in the next 30 days.")
    section("2.", "Leave balances")
    rows = []
    for m in members:
        b = pdb.balance_for(m["email"])
        rows.append({"Name": m["full_name"], "Code": m["emp_code"],
                     **{f"{k} available": (v["available"] if v["available"] is not None else "—")
                        for k, v in b.items() if v.get("available") is not None},
                     **{f"{k} taken": v["used"] for k, v in b.items()}})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


# ===================================================================== HR admin
EMP_COLUMNS = ["Employee code", "Full name", "Work email", "Designation", "Department", "Location", "Work state",
               "Date of joining (DD/MM/YYYY)", "Manager email", "Date of birth (DD/MM/YYYY)", "Gender", "Phone", "UAN",
               "PAN", "Bank account", "IFSC", "Bank name"]


def _date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "NaT", "nan"):
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.date()
    if isinstance(v, date):
        return v
    return pd.to_datetime(str(v).strip(), dayfirst=True).date()


def employees_admin(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("emp_msg")
    if "emp_pw" in st.session_state:
        rows = st.session_state.pop("emp_pw")
        st.success("Logins created. Temporary passwords are shown **only now** – share each one privately. "
                   "Everyone must choose their own password at first sign-in.")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.download_button("Download these temporary passwords (Excel) – delete the file after sharing",
                           excel_bytes({"Temporary passwords": pd.DataFrame(rows)}), "new_logins.xlsx")
    emps = pdb.list_employees()
    names = {e["email"]: e["full_name"] for e in emps}
    section("1.", f"Employees ({len([e for e in emps if not e['date_of_exit'] or e['date_of_exit'] >= today_ist()])} active)")
    if emps:
        st.dataframe(pd.DataFrame([{
            "Code": e["emp_code"], "Name": e["full_name"], "Email": e["email"], "Designation": e["designation"] or "",
            "Department": e["department"] or "", "Manager": names.get(e["manager_email"], e["manager_email"] or ""),
            "Joined": d(e["date_of_joining"]), "Location": e["location"] or "",
            "Status": "Exited " + d(e["date_of_exit"]) if e["date_of_exit"] and e["date_of_exit"] < today_ist()
            else ("Leaving " + d(e["date_of_exit"]) if e["date_of_exit"] else "Active"),
        } for e in emps]), hide_index=True, width="stretch")

    add_tab, edit_tab, bulk_tab = st.tabs(["Add employee", "Edit / exit employee", "Bulk upload (Excel)"])
    with add_tab:
        employee_form(ctx, None, emps)
    with edit_tab:
        if not emps:
            st.caption("No employees yet.")
        else:
            labels = {f"{e['full_name']} ({e['emp_code']}) <{e['email']}>": e for e in emps}
            pick = st.selectbox("Employee", list(labels), key="emp_edit_pick")
            employee_form(ctx, labels[pick], emps)
    with bulk_tab:
        bulk_upload(ctx)


def employee_form(ctx, e, emps):
    pdb, me = ctx.pdb, ctx.user
    new = e is None
    e = e or {}
    priv = e.get("private") or {}
    mgr_opts = [""] + [f"{x['full_name']} <{x['email']}>" for x in emps if x["email"] != e.get("email")]
    cur_mgr = next((o for o in mgr_opts if e.get("manager_email") and o.endswith(f"<{e['manager_email']}>")), "")
    with st.form(f"emp_form_{e.get('email', 'new')}", clear_on_submit=new):
        c1, c2, c3 = st.columns(3)
        full_name = c1.text_input("Full name", e.get("full_name", ""))
        email = c2.text_input("Work email (sign-in)", e.get("email", ""), disabled=not new)
        code = c3.text_input("Employee code", e.get("emp_code", ""))
        c4, c5, c6 = st.columns(3)
        desig = c4.text_input("Designation", e.get("designation") or "")
        dept = c5.text_input("Department", e.get("department") or "")
        mgr = c6.selectbox("Hardline manager", mgr_opts, index=mgr_opts.index(cur_mgr))
        c7, c8, c9 = st.columns(3)
        doj = c7.date_input("Date of joining", e.get("date_of_joining") or today_ist(), format="DD/MM/YYYY",
                            min_value=date(1980, 1, 1))
        loc = c8.text_input("Location (city)", e.get("location") or "")
        state = c9.selectbox("Work state (for Professional Tax)", STATES,
                             index=STATES.index(e.get("work_state")) if e.get("work_state") in STATES else 0)
        c10, c11, c12 = st.columns(3)
        dob = c10.date_input("Date of birth", e.get("date_of_birth"), format="DD/MM/YYYY",
                             min_value=date(1940, 1, 1), max_value=today_ist())
        gender = c11.selectbox("Gender", ["", "Female", "Male", "Other"],
                               index=["", "Female", "Male", "Other"].index(e.get("gender") or ""))
        phone = c12.text_input("Phone", e.get("phone") or "")
        if ctx.vault_ready:
            st.markdown("**Statutory and bank details** (stored encrypted)")
            c13, c14, c15, c16, c17 = st.columns(5)
            uan = c13.text_input("UAN", e.get("uan") or "")
            pan = c14.text_input("PAN", priv.get("pan", ""))
            acct = c15.text_input("Bank account no.", priv.get("bank_account", ""), type="password")
            ifsc = c16.text_input("IFSC", priv.get("ifsc", ""))
            bank = c17.text_input("Bank name", priv.get("bank_name", ""))
        else:
            uan, pan, acct, ifsc, bank = e.get("uan") or "", None, None, None, None
            st.caption("PAN and bank details are hidden until the encryption key is set in the app's secrets.")
        exit_date = None
        if not new:
            exit_date = st.date_input("Date of exit (leave empty if still working)", e.get("date_of_exit"),
                                      format="DD/MM/YYYY", min_value=date(1980, 1, 1))
        give_login = st.checkbox("Create a sign-in for this employee", value=True) if new else False
        if st.form_submit_button("Add Employee" if new else "Save Changes", width="stretch"):
            data = {"email": email if new else e["email"], "full_name": full_name, "emp_code": code,
                    "designation": desig, "department": dept, "location": loc, "work_state": state,
                    "date_of_joining": doj, "date_of_exit": exit_date, "date_of_birth": dob, "gender": gender,
                    "phone": phone, "uan": uan,
                    "manager_email": mgr.rsplit("<", 1)[1].rstrip(">") if mgr else None}
            if ctx.vault_ready:
                data["private"] = {"pan": (pan or "").strip().upper(), "bank_account": (acct or "").strip(),
                                   "ifsc": (ifsc or "").strip().upper(), "bank_name": (bank or "").strip()}
            err = pdb.save_employee(data, by=me["email"], new=new)
            if err:
                st.error(err)
                return
            who = data["email"].strip().lower()
            ctx.audit("employee_added" if new else "employee_updated", f"{full_name} ({code})", who)
            msgs = [f"{'Added' if new else 'Saved'} {full_name}."]
            if new and give_login and not pdb.get_user(who):
                pw = temporary_password()
                pdb.create_user(who, full_name, [], pw, created_by=me["email"])
                ctx.audit("user_added", f"{full_name} <{who}> as Resource", who)
                st.session_state["emp_pw"] = [{"Name": full_name, "Email": who, "Temporary password": pw}]
            if not new and exit_date and exit_date <= today_ist():
                if pdb.get_user(who) and pdb.get_user(who)["active"]:
                    pdb.update_user(who, active=0)
                    ctx.audit("user_deactivated", f"{full_name} (exit {exit_date})", who)
                    msgs.append("Their sign-in has been switched off because the exit date has passed.")
            st.session_state["emp_msg"] = " ".join(msgs)
            st.rerun()


def bulk_upload(ctx):
    pdb, me = ctx.pdb, ctx.user
    st.caption("For adding many employees at once. Download the template, fill one row per employee, upload it. "
               "Managers can be in the same file. Existing employees (same email) are updated; new ones get a "
               "sign-in with a temporary password.")
    st.download_button("Download template (Excel)", excel_bytes({"Employees": pd.DataFrame(columns=EMP_COLUMNS)}),
                       "employee_template.xlsx")
    up = st.file_uploader("Filled template", type=["xlsx"], key="emp_bulk")
    if up is None:
        return
    try:
        df = pd.read_excel(up, dtype=str).fillna("")
    except Exception as exc:
        st.error(f"Could not read the file: {exc}")
        return
    missing = [c for c in EMP_COLUMNS[:4] if c not in df.columns]
    if missing:
        st.error("Missing columns: " + ", ".join(missing) + ". Please use the template.")
        return
    st.dataframe(df.head(20), hide_index=True, width="stretch")
    st.caption(f"{len(df)} row(s) found.")
    if not st.button("Import Employees", width="stretch"):
        return
    results, new_logins = [], []
    rows = []
    for i, r in df.iterrows():
        try:
            rows.append({"email": r["Work email"].strip().lower(), "full_name": r["Full name"].strip(),
                         "emp_code": r["Employee code"].strip(), "designation": r.get("Designation", ""),
                         "department": r.get("Department", ""), "location": r.get("Location", ""),
                         "work_state": r.get("Work state", ""), "phone": r.get("Phone", ""), "uan": r.get("UAN", ""),
                         "date_of_joining": _date(r.get("Date of joining (DD/MM/YYYY)")),
                         "date_of_birth": _date(r.get("Date of birth (DD/MM/YYYY)")), "gender": r.get("Gender", ""),
                         "manager_email": r.get("Manager email", "").strip().lower() or None,
                         "private": {"pan": r.get("PAN", "").strip().upper(), "bank_account": r.get("Bank account", "").strip(),
                                     "ifsc": r.get("IFSC", "").strip().upper(), "bank_name": r.get("Bank name", "").strip()}
                         if ctx.vault_ready else None, "_row": i + 2})
        except Exception as exc:
            results.append({"Row": i + 2, "Employee": r.get("Full name", ""), "Result": f"Not imported: {exc}"})
    # pass 1 without managers (so managers in the same file exist), pass 2 sets managers
    ok = []
    for row in rows:
        exists = pdb.get_employee(row["email"])
        data = {**row, "manager_email": exists["manager_email"] if exists else None}
        err = pdb.save_employee(data, by=me["email"], new=not exists)
        if err:
            results.append({"Row": row["_row"], "Employee": row["full_name"], "Result": f"Not imported: {err}"})
            continue
        ok.append(row)
        if not pdb.get_user(row["email"]):
            pw = temporary_password()
            pdb.create_user(row["email"], row["full_name"], [], pw, created_by=me["email"])
            new_logins.append({"Name": row["full_name"], "Email": row["email"], "Temporary password": pw})
    for row in ok:
        if row["manager_email"]:
            data = {**row, "private": None}
            err = pdb.save_employee(data, by=me["email"], new=False)
            if err:
                results.append({"Row": row["_row"], "Employee": row["full_name"],
                                "Result": f"Imported, but manager not set: {err}"})
                continue
        results.append({"Row": row["_row"], "Employee": row["full_name"], "Result": "Imported"})
    ctx.audit("employees_imported", f"{len(ok)} of {len(df)} row(s) imported; {len(new_logins)} new sign-in(s)")
    st.dataframe(pd.DataFrame(sorted(results, key=lambda x: x["Row"])), hide_index=True, width="stretch")
    if new_logins:
        st.session_state["emp_pw"] = new_logins
        st.session_state["emp_msg"] = f"Imported {len(ok)} employee(s)."
        st.rerun()


def leave_admin(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("ladm_msg")
    emps = pdb.list_employees()
    names = {e["email"]: e["full_name"] for e in emps}
    section("1.", "All leave requests")
    c1, c2, c3, c4 = st.columns(4)
    status = c1.multiselect("Status", ["Pending", "Approved", "Rejected", "Cancelled"], key="la_status")
    who = c2.selectbox("Employee", ["Everyone"] + [f"{e['full_name']} <{e['email']}>" for e in emps], key="la_who")
    since = c3.date_input("From", date(today_ist().year, 1, 1), format="DD/MM/YYYY", key="la_from")
    until = c4.date_input("To", date(today_ist().year, 12, 31), format="DD/MM/YYYY", key="la_to")
    email = None if who == "Everyone" else who.rsplit("<", 1)[1].rstrip(">")
    reqs = pdb.leave_requests(email=email, statuses=status or None, since=since, until=until)
    if reqs:
        table = pd.DataFrame([{"ID": r["id"], "Name": names.get(r["employee_email"], r["employee_email"]),
                               "Type": r["leave_type"], "From": d(r["start_date"]), "To": d(r["end_date"]),
                               "Days": r["days"], "Status": r["status"], "Approver": r["approver_email"] or "",
                               "Decided by": r["decided_by"] or "", "Reason": r["reason"] or ""} for r in reqs])
        st.dataframe(table, hide_index=True, width="stretch")
        st.download_button("Download (Excel)", excel_bytes({"Leave requests": table}), "leave_requests.xlsx",
                           on_click=ctx.audit, args=("report_exported", "Leave requests"))
        active = [r for r in reqs if r["status"] in ("Pending", "Approved")]
        if active:
            with st.form("hr_cancel"):
                labels = {f"#{r['id']} {names.get(r['employee_email'])} {r['leave_type']} {d(r['start_date'])}–"
                          f"{d(r['end_date'])} ({r['status']})": r for r in active}
                pick = st.selectbox("Cancel a request on someone's behalf (e.g. leave not taken)", list(labels))
                if st.form_submit_button("Cancel Request"):
                    r = labels[pick]
                    err = pdb.cancel_leave(r["id"], me["email"], as_hr=True)
                    if err:
                        st.error(err)
                    else:
                        ctx.audit("leave_cancelled", f"HR cancelled #{r['id']} for {names.get(r['employee_email'])}",
                                  r["id"])
                        st.session_state["ladm_msg"] = "Request cancelled."
                        st.rerun()
    else:
        st.caption("No requests for these filters.")

    section("2.", "Balance report (everyone)")
    types = pdb.leave_types()
    rows = []
    for e in emps:
        b = pdb.balance_for(e["email"])
        row = {"Code": e["emp_code"], "Name": e["full_name"], "Joined": d(e["date_of_joining"])}
        for t in types:
            v = b.get(t.code) or {}
            if t.paid:
                row[f"{t.code} available"] = v.get("available", 0)
            row[f"{t.code} taken"] = v.get("used", 0)
        rows.append(row)
    if rows:
        bt = pd.DataFrame(rows)
        st.dataframe(bt, hide_index=True, width="stretch")
        st.download_button("Download balances (Excel)", excel_bytes({"Leave balances": bt}), "leave_balances.xlsx",
                           on_click=ctx.audit, args=("report_exported", "Leave balances"))

    section("3.", "Adjust a balance")
    st.caption("Use this for opening balances when you start using the Portal, or for corrections "
               "(e.g. +2 PL compensatory off, or -1 CL). Each adjustment is kept with the reason and who made it.")
    if emps:
        with st.form("adjust", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
            who = c1.selectbox("Employee", [f"{e['full_name']} <{e['email']}>" for e in emps])
            lt = c2.selectbox("Leave type", [t.code for t in types if t.paid])
            days = c3.number_input("Days (+ add / − deduct)", -100.0, 100.0, 0.0, 0.5)
            eff = c4.date_input("Effective date", today_ist(), format="DD/MM/YYYY")
            reason = st.text_input("Reason", placeholder="e.g. Opening balance as on 1 Nov 2026")
            if st.form_submit_button("Save Adjustment"):
                if not days or not reason.strip():
                    st.error("Enter the days and a reason.")
                else:
                    email = who.rsplit("<", 1)[1].rstrip(">")
                    pdb.add_adjustment(email, lt, days, eff, reason.strip(), me["email"])
                    ctx.audit("leave_adjusted", f"{names.get(email)}: {days:+g} {lt} ({reason.strip()})", email)
                    pdb.notify(email, f"HR adjusted your {lt} balance by {days:+g} day(s): {reason.strip()}")
                    st.session_state["ladm_msg"] = f"Adjustment saved: {days:+g} {lt} for {names.get(email)}."
                    st.rerun()


def policy_admin(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("padm_msg")
    section("1.", "Publish a policy")
    current = pdb.list_policies()
    with st.form("policy_add", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 1, 1])
        title = c1.text_input("Title", placeholder="e.g. Leave Policy")
        cat = c2.selectbox("Category", ["Leave", "Attendance", "Code of Conduct", "IT & Security", "Travel",
                                        "Compensation & Benefits", "POSH", "General"])
        eff = c3.date_input("Effective from", today_ist(), format="DD/MM/YYYY")
        summary = st.text_area("Short summary (shown to employees)", max_chars=600)
        replaces = st.selectbox("Replaces an existing policy? (keeps it as an older version)",
                                ["No – new policy"] + [f"{p['title']} (v{p['version']}) #{p['id']}" for p in current])
        f = st.file_uploader("Policy document (PDF or Word)", type=["pdf", "docx"])
        if st.form_submit_button("Publish Policy", width="stretch"):
            if not title.strip() or f is None:
                st.error("Enter a title and attach the document.")
            else:
                rep = None if replaces.startswith("No") else int(replaces.rsplit("#", 1)[1])
                pdb.add_policy(title, cat, summary.strip(), eff, f.name, f.getvalue(), me["email"], rep)
                ctx.audit("policy_published", title.strip())
                for u in pdb.list_users():
                    if u["active"]:
                        pdb.notify(u["email"], f"New HR policy published: {title.strip()}. Please read and "
                                               f"acknowledge it in HR > Policies.")
                st.session_state["padm_msg"] = f"Published '{title.strip()}'. Everyone has been notified."
                st.rerun()
    section("2.", "Acknowledgements")
    if not current:
        st.caption("No policies yet.")
        return
    active_emps = pdb.list_employees(include_exited=False)
    pick = st.selectbox("Policy", [f"{p['title']} (v{p['version']}) #{p['id']}" for p in current], key="ack_pick")
    pid = int(pick.rsplit("#", 1)[1])
    done = {a["user_email"]: a["at"] for a in pdb.acks(pid=pid)}
    rows = [{"Name": e["full_name"], "Email": e["email"], "Acknowledged": "Yes" if e["email"] in done else "Not yet",
             "On": ist(done.get(e["email"]))} for e in active_emps]
    st.caption(f"{len([r for r in rows if r['Acknowledged'] == 'Yes'])} of {len(rows)} employees have acknowledged.")
    if rows:
        at = pd.DataFrame(rows)
        st.dataframe(at, hide_index=True, width="stretch")
        st.download_button("Download (Excel)", excel_bytes({"Acknowledgements": at}), "policy_acknowledgements.xlsx")
    if st.button("Retire this policy (hide from employees)", key=f"retire_{pid}"):
        pdb.retire_policy(pid)
        ctx.audit("policy_retired", pick)
        st.session_state["padm_msg"] = "Policy retired."
        st.rerun()


def rules_admin(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("rules_msg")
    section("1.", "Leave rules")
    st.caption("Changes apply to everyone's balance straight away, because balances are calculated from these "
               "rules. 'Carry-forward cap' only matters for leave that does not lapse.")
    types = pdb.leave_types(active_only=False)
    df = pd.DataFrame([{"Code": t.code, "Name": t.name, "Credited per month": t.monthly_accrual,
                        "Lapses on 31 Dec": t.lapse_year_end, "Carry-forward cap (days)": t.carry_forward_cap,
                        "Paid": t.paid} for t in types])
    edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key="lt_editor",
                            column_config={"Code": st.column_config.TextColumn(max_chars=10, required=True)})
    if st.button("Save Leave Rules"):
        codes = set()
        for _, r in edited.iterrows():
            code = str(r["Code"] or "").strip().upper()
            if not code:
                continue
            codes.add(code)
            pdb.save_leave_type(code, str(r["Name"] or code), float(r["Credited per month"] or 0),
                                float(r["Carry-forward cap (days)"] or 0), bool(r["Lapses on 31 Dec"]),
                                bool(r["Paid"]), True)
        for t in types:
            if t.code not in codes:
                pdb.save_leave_type(t.code, t.name, t.monthly_accrual, t.carry_forward_cap, t.lapse_year_end,
                                    t.paid, active=False)
        ctx.audit("leave_rules_changed", "; ".join(f"{r['Code']}: {r['Credited per month']}/month"
                                                    for _, r in edited.iterrows()))
        st.session_state["rules_msg"] = "Leave rules saved."
        st.rerun()

    section("2.", "Holiday calendar")
    year = st.selectbox("Year", [today_ist().year, today_ist().year + 1], key="hol_admin_year")
    hol = pdb.holiday_list(year)
    hdf = pd.DataFrame([{"Date": h["day"], "Holiday": h["name"]} for h in hol] or
                       [{"Date": None, "Holiday": ""}])
    hed = st.data_editor(hdf, num_rows="dynamic", hide_index=True, width="stretch", key=f"hol_ed_{year}",
                         column_config={"Date": st.column_config.DateColumn(format="DD/MM/YYYY")})
    if st.button("Save Holidays", key=f"hol_save_{year}"):
        rows = [{"day": _date(r["Date"]), "name": str(r["Holiday"] or "Holiday")} for _, r in hed.iterrows()
                if _date(r["Date"])]
        bad = [r for r in rows if r["day"].year != year]
        if bad:
            st.error(f"All dates must be in {year}.")
        else:
            pdb.set_holidays(year, rows)
            ctx.audit("holidays_changed", f"{year}: {len(rows)} holiday(s)")
            st.session_state["rules_msg"] = f"Saved {len(rows)} holiday(s) for {year}."
            st.rerun()
