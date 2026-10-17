"""Finance tab of the Ciliconchip Portal: own payslips and tax choice for everyone, payroll for Finance Admins."""

import io
import zipfile
from datetime import date, datetime

import pandas as pd
import streamlit as st

from .payroll import EARNING_FIELDS, TAX_RULES, annual_tax, compute_payslip, fy_of, fy_months, taxable_income
from .payslip import payslip_pdf
from .portal_db import today_ist
from .security import mask
from .ui_common import card, d, excel_bytes, flash, inr, ist, section

PF_MODES = ["Capped at PF wage ceiling", "On full basic", "Not applicable"]
DECL_FIELDS = [("sec_80c", "80C investments (PPF, ELSS, LIC, tuition, home-loan principal) – excluding PF"),
               ("sec_80d", "80D medical insurance premium"),
               ("sec_80ccd_1b", "80CCD(1B) own NPS contribution (max 50,000)"),
               ("home_loan_interest", "Home-loan interest, self-occupied (max 2,00,000)"),
               ("rent_paid_annual", "Rent paid for the year (for HRA exemption)"),
               ("other_deductions", "Other deductions (e.g. 80E education-loan interest)")]


def _age(dob, on):
    if not dob:
        return 30
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def render(ctx):
    if not ctx.vault_ready:
        st.warning("Finance is switched off until the encryption key (PORTAL_ENCRYPTION_KEY) is added to the "
                   "app's secrets. Salary data is always stored encrypted. See the README for the one-time setup.")
        return
    names = ["My Payslips", "My Tax Choice"]
    if ctx.is_fin:
        names += ["Salary Structures", "Run Payroll", "Tax Regimes", "Reports", "Finance Settings"]
    tabs = dict(zip(names, st.tabs(names)))
    with tabs["My Payslips"]:
        my_payslips(ctx)
    with tabs["My Tax Choice"]:
        my_tax(ctx)
    if ctx.is_fin:
        with tabs["Salary Structures"]:
            salary_admin(ctx)
        with tabs["Run Payroll"]:
            payroll_run(ctx)
        with tabs["Tax Regimes"]:
            tax_admin(ctx)
        with tabs["Reports"]:
            reports(ctx)
        with tabs["Finance Settings"]:
            settings_admin(ctx)


# ===================================================================== self-service
def slip_view(slip):
    c1, c2, c3 = st.columns(3)
    with c1:
        card(inr(slip["gross"]), "GROSS EARNINGS")
    with c2:
        card(inr(slip["total_deductions"]), "TOTAL DEDUCTIONS")
    with c3:
        card(inr(slip["net"]), "NET PAY", "#087A4A")
    a, b = st.columns(2)
    with a:
        st.dataframe(pd.DataFrame([{"Earnings": k, "Amount": inr(v)} for k, v in slip["earnings"].items()]),
                     hide_index=True, width="stretch")
    with b:
        st.dataframe(pd.DataFrame([{"Deductions": k, "Amount": inr(v)} for k, v in slip["deductions"].items()]
                                  or [{"Deductions": "—", "Amount": ""}]), hide_index=True, width="stretch")
    st.caption(f"Paid days {slip['paid_days']:g} of {slip['days_in_month']}"
               + (f" (loss of pay {slip['lwp_days']:g} day(s))" if slip.get("lwp_days") else "")
               + f" · Tax regime: {'New' if slip['regime'] == 'new' else 'Old'}"
               + (" · Employer contributions: " + ", ".join(f"{k} {inr(v)}" for k, v in slip["employer"].items())
                  if slip.get("employer") else ""))


def my_payslips(ctx):
    pdb, me = ctx.pdb, ctx.user
    if not ctx.emp:
        st.info("Your employee record has not been set up yet. Please ask HR.")
        return
    slips = pdb.my_payslips(me["email"])
    section("1.", "My payslips")
    if not slips:
        st.caption("No payslips published for you yet.")
        return
    labels = {datetime.strptime(s["month"], "%Y-%m").strftime("%B %Y"): s for s in slips}
    pick = st.selectbox("Month", list(labels), key="my_slip_month")
    s = labels[pick]
    slip_view(s["data"])
    st.download_button(f"Download payslip – {pick} (PDF)",
                       payslip_pdf(s["data"], ctx.emp, ctx.settings["company_name"], ctx.settings["company_address"]),
                       f"Payslip_{ctx.emp['emp_code']}_{s['month']}.pdf", "application/pdf",
                       on_click=ctx.audit, args=("payslip_downloaded", f"own payslip {s['month']}", me["email"]))
    fy = fy_of(today_ist())
    ytd = [x for x in slips if x["data"].get("fy") == fy]
    if ytd:
        st.caption(f"Tax year {fy} so far: gross {inr(sum(x['data']['gross'] for x in ytd))}, tax deducted "
                   f"{inr(sum(x['data']['deductions'].get('Income Tax (TDS)', 0) for x in ytd))}, net paid "
                   f"{inr(sum(x['data']['net'] for x in ytd))}.")


def my_tax(ctx):
    pdb, me = ctx.pdb, ctx.user
    if not ctx.emp:
        st.info("Your employee record has not been set up yet. Please ask HR.")
        return
    flash("tax_msg")
    fy = fy_of(today_ist())
    choice = pdb.tax_choice(me["email"], fy)
    locked = bool(float(ctx.settings.get("lock_tax_choice", 0) or 0))
    section("1.", f"My tax regime and declarations – tax year {fy}")
    struct = pdb.salary_on(me["email"], today_ist())
    if struct:
        annual = sum(float(struct.get(k) or 0) for k, _ in EARNING_FIELDS) * 12
        basic, hra = float(struct.get("basic") or 0) * 12, float(struct.get("hra") or 0) * 12
        pfv = (min(float(struct.get("basic") or 0), float(ctx.settings["pf_wage_ceiling"]))
               if struct.get("pf_mode") != "On full basic" else float(struct.get("basic") or 0)) * 0.12 * 12 \
            if struct.get("pf_mode") != "Not applicable" else 0
        cmp = []
        for reg in ("new", "old"):
            ti = taxable_income(annual, reg, fy, choice["decl"], basic, hra, pfv, 2400)
            cmp.append({"Regime": "New" if reg == "new" else "Old", "Taxable income": inr(ti["taxable"]),
                        "Estimated tax for the year": inr(annual_tax(ti["taxable"], reg, fy,
                                                                     _age(ctx.emp.get("date_of_birth"),
                                                                          today_ist()))["total"])})
        st.dataframe(pd.DataFrame(cmp), hide_index=True, width="stretch")
        st.caption("Estimate on a full year of your current salary and the declarations below. Your actual tax "
                   "depends on the whole year's pay.")
    with st.form("my_tax"):
        regime = st.radio("Tax regime", ["New regime", "Old regime"], horizontal=True,
                          index=0 if choice["regime"] == "new" else 1, disabled=locked)
        st.caption("Declarations count only under the old regime.")
        vals = {}
        cols = st.columns(2)
        for i, (k, label) in enumerate(DECL_FIELDS):
            vals[k] = cols[i % 2].number_input(label, 0.0, 10_000_000.0, float(choice["decl"].get(k) or 0), 1000.0,
                                               disabled=locked)
        vals["metro_city"] = st.checkbox("I live in Delhi, Mumbai, Kolkata or Chennai (metro rate for HRA)",
                                         bool(choice["decl"].get("metro_city")), disabled=locked)
        if st.form_submit_button("Save My Choice", disabled=locked):
            reg = "new" if regime.startswith("New") else "old"
            pdb.save_tax_choice(me["email"], fy, reg, vals, me["email"])
            ctx.audit("tax_choice_saved", f"{fy}: {reg} regime", me["email"])
            for f in pdb.users_with_role("Finance Admin"):
                pdb.notify(f, f"{me['full_name']} updated their tax choice for {fy} ({reg} regime).")
            st.session_state["tax_msg"] = "Saved. Finance will use it from the next payroll."
            st.rerun()
    if locked:
        st.caption("Finance has locked tax choices for this year. Please contact Finance for changes.")
    if choice["updated_at"]:
        st.caption(f"Last updated {ist(choice['updated_at'])} by {choice['updated_by']}.")


# ===================================================================== finance admin
def _emp_pick(pdb, key, include_exited=False):
    emps = pdb.list_employees(include_exited=include_exited)
    if not emps:
        st.caption("No employees yet – HR adds them under HR > Employees.")
        return None, []
    labels = {f"{e['full_name']} ({e['emp_code']})": e for e in emps}
    return labels[st.selectbox("Employee", list(labels), key=key)], emps


def salary_admin(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("sal_msg")
    section("1.", "Salary structure")
    e, emps = _emp_pick(pdb, "sal_emp")
    if not e:
        return
    hist = pdb.salary_history(e["email"])
    cur = hist[0]["data"] if hist else {}
    if hist:
        st.caption(f"Current structure effective from {d(hist[0]['effective_from'])}. History is kept; a new "
                   f"structure applies from its effective date.")
    with st.form(f"sal_form_{e['email']}"):
        st.markdown("**Monthly earnings (₹)**")
        cols = st.columns(len(EARNING_FIELDS))
        vals = {}
        for col, (k, label) in zip(cols, EARNING_FIELDS):
            vals[k] = col.number_input(label, 0.0, 10_000_000.0, float(cur.get(k) or 0), 500.0)
        st.markdown("**Deductions**")
        c1, c2, c3, c4 = st.columns(4)
        vals["pf_mode"] = c1.selectbox("PF", PF_MODES, index=PF_MODES.index(cur.get("pf_mode") or PF_MODES[0]))
        vals["vpf"] = c2.number_input("VPF per month (₹)", 0.0, 1_000_000.0, float(cur.get("vpf") or 0), 500.0)
        pt_txt = c3.text_input("Professional Tax per month (₹) – blank = by state rule",
                               "" if cur.get("pt") in (None, "") else f"{float(cur['pt']):g}")
        tds_txt = c4.text_input("Fixed TDS per month (₹) – blank = calculate", "" if cur.get("tds_override") in
                                (None, "") else f"{float(cur['tds_override']):g}")
        c5, c6 = st.columns([1, 2])
        vals["other_deduction"] = c5.number_input("Recurring other deduction (₹/month)", 0.0, 1_000_000.0,
                                                  float(cur.get("other_deduction") or 0), 100.0)
        vals["other_deduction_label"] = c6.text_input("Name of that deduction", cur.get("other_deduction_label")
                                                      or "", placeholder="e.g. Salary advance recovery")
        eff = st.date_input("Effective from", date(today_ist().year, today_ist().month, 1), format="DD/MM/YYYY")
        if st.form_submit_button("Save Salary Structure", width="stretch"):
            try:
                vals["pt"] = None if not pt_txt.strip() else float(pt_txt)
                vals["tds_override"] = None if not tds_txt.strip() else float(tds_txt)
            except ValueError:
                st.error("Professional Tax and fixed TDS must be numbers (or left blank).")
                return
            if not vals["basic"]:
                st.error("Enter at least the Basic salary.")
                return
            pdb.save_salary(e["email"], eff, vals, me["email"])
            ctx.audit("salary_saved", f"{e['full_name']} structure effective {eff}", e["email"])
            st.session_state["sal_msg"] = f"Salary structure saved for {e['full_name']} from {d(eff)}."
            st.rerun()
    if cur:
        gross = sum(float(cur.get(k) or 0) for k, _ in EARNING_FIELDS)
        pf_base = 0 if cur.get("pf_mode") == "Not applicable" else (
            float(cur.get("basic") or 0) if cur.get("pf_mode") == "On full basic"
            else min(float(cur.get("basic") or 0), float(ctx.settings["pf_wage_ceiling"])))
        er_pf = round(pf_base * float(ctx.settings["pf_rate"]))
        c1, c2, c3 = st.columns(3)
        with c1:
            card(inr(gross), "MONTHLY GROSS")
        with c2:
            card(inr(er_pf), "EMPLOYER PF / MONTH")
        with c3:
            card(inr((gross + er_pf) * 12), "ANNUAL CTC (GROSS + EMPLOYER PF)")
        if float(cur.get("basic") or 0) < 0.5 * gross:
            st.caption("Note: Basic is below 50% of gross. Under the new labour codes, allowances above 50% of pay "
                       "may be counted as wages for PF and gratuity – please confirm with your payroll adviser.")
    if hist:
        with st.expander(f"History ({len(hist)})"):
            st.dataframe(pd.DataFrame([{"Effective from": d(h["effective_from"]),
                                        "Gross / month": inr(sum(float(h["data"].get(k) or 0) for k, _ in EARNING_FIELDS)),
                                        "Basic": inr(h["data"].get("basic")), "Saved by": h["created_by"],
                                        "Saved on": ist(h["created_at"])} for h in hist]),
                         hide_index=True, width="stretch")

    section("2.", "Bulk upload salary structures (Excel)")
    cols = ["Work email"] + [label for _, label in EARNING_FIELDS] + ["PF (Capped / Full / None)", "VPF",
                                                                      "Professional Tax (blank = state rule)",
                                                                      "Effective from (DD/MM/YYYY)"]
    template = pd.DataFrame([{**{c: "" for c in cols}, "Work email": x["email"]} for x in emps], columns=cols)
    st.download_button("Download template with all employees", excel_bytes({"Salaries": template}),
                       "salary_template.xlsx")
    up = st.file_uploader("Filled template", type=["xlsx"], key="sal_bulk")
    if up is not None and st.button("Import Salary Structures"):
        df = pd.read_excel(up, dtype=str).fillna("")
        known = {x["email"] for x in emps}
        done, errors = 0, []
        pfmap = {"capped": PF_MODES[0], "full": PF_MODES[1], "none": PF_MODES[2], "": PF_MODES[0]}
        for i, r in df.iterrows():
            email = str(r.get("Work email", "")).strip().lower()
            try:
                if email not in known:
                    raise ValueError("unknown employee email")
                vals = {k: float(r.get(label) or 0) for k, label in EARNING_FIELDS}
                if not vals["basic"]:
                    continue
                vals["pf_mode"] = pfmap.get(str(r.get("PF (Capped / Full / None)", "")).strip().lower().split(" ")[0]
                                            if str(r.get("PF (Capped / Full / None)", "")).strip() else "", PF_MODES[0])
                vals["vpf"] = float(r.get("VPF") or 0)
                pt = str(r.get("Professional Tax (blank = state rule)", "")).strip()
                vals["pt"] = float(pt) if pt else None
                vals["tds_override"] = None
                vals["other_deduction"], vals["other_deduction_label"] = 0.0, ""
                eff = pd.to_datetime(str(r.get("Effective from (DD/MM/YYYY)")).strip(), dayfirst=True).date()
                pdb.save_salary(email, eff, vals, me["email"])
                done += 1
            except Exception as exc:
                errors.append(f"Row {i + 2} ({email}): {exc}")
        ctx.audit("salaries_imported", f"{done} salary structure(s) imported")
        st.success(f"Imported {done} salary structure(s).")
        for err in errors:
            st.error(err)


def _compute_month(ctx, month_start: date, extras: dict):
    pdb, settings = ctx.pdb, ctx.settings
    month = month_start.strftime("%Y-%m")
    fy = fy_of(month_start)
    slips, problems = {}, []
    month_end = date(month_start.year + (month_start.month == 12), month_start.month % 12 + 1, 1)
    for e in pdb.list_employees():
        if e["date_of_joining"] >= month_end or (e["date_of_exit"] and e["date_of_exit"] < month_start):
            continue
        struct = pdb.salary_on(e["email"], max(month_start, e["date_of_joining"]))
        if not struct:
            problems.append(f"{e['full_name']} ({e['emp_code']}): no salary structure – not included.")
            continue
        tax = pdb.tax_choice(e["email"], fy)
        ex = extras.get(e["email"]) or {}
        slip = compute_payslip(struct, month_start, e["date_of_joining"], e["date_of_exit"],
                               pdb.lwp_days(e["email"], month_start.year, month_start.month), settings,
                               tax["regime"], tax["decl"], pdb.ytd(e["email"], fy, month),
                               float(ex.get("earning") or 0), ex.get("earning_label") or "",
                               float(ex.get("deduction") or 0), ex.get("deduction_label") or "",
                               e.get("work_state") or "", _age(e.get("date_of_birth"), month_start))
        slips[e["email"]] = slip
    return slips, problems


def payroll_run(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("run_msg")
    section("1.", "Choose the month")
    t = today_ist()
    months = [date(t.year - (t.month <= i), (t.month - i - 1) % 12 + 1, 1) for i in range(0, 6)]
    months = sorted(set(months + [date(t.year + (t.month == 12), t.month % 12 + 1, 1)]), reverse=True)
    m = st.selectbox("Payroll month", months, index=months.index(date(t.year, t.month, 1)),
                     format_func=lambda x: x.strftime("%B %Y"), key="run_month")
    month = m.strftime("%Y-%m")
    run = pdb.get_run(month)
    emps = {e["email"]: e for e in pdb.list_employees()}
    earlier = [r for r in pdb.list_runs() if r["month"] < month and r["status"] == "Draft"
               and fy_of(datetime.strptime(r["month"], "%Y-%m").date()) == fy_of(m)]
    if earlier:
        st.warning("Earlier months of this tax year are still drafts (" + ", ".join(r["month"] for r in earlier)
                   + "). Publish them first so this month's tax uses the right year-to-date figures.")

    if run and run["status"] == "Published":
        st.success(f"{m:%B %Y} payroll was published on {ist(run['published_at'])} by {run['published_by']}. "
                   f"Employees can see their payslips. Corrections go into next month as arrears or deductions.")
        slips = pdb.run_slips(run["id"])
        _run_table(slips, emps)
        return

    section("2.", "One-off amounts for this month (optional)")
    prev = {s["employee_email"]: s["extra"] for s in pdb.run_slips(run["id"])} if run else {}
    xdf = pd.DataFrame([{"Employee": f"{e['full_name']} ({e['emp_code']})", "Email": e["email"],
                         "Additional earning (₹)": float((prev.get(e["email"]) or {}).get("earning") or 0),
                         "Earning name": (prev.get(e["email"]) or {}).get("earning_label") or "",
                         "Additional deduction (₹)": float((prev.get(e["email"]) or {}).get("deduction") or 0),
                         "Deduction name": (prev.get(e["email"]) or {}).get("deduction_label") or ""}
                        for e in emps.values()
                        if not (e["date_of_exit"] and e["date_of_exit"] < m)])
    st.caption("e.g. joining bonus, arrears, reimbursements, or a one-time recovery. Leave as 0 when not needed.")
    xed = st.data_editor(xdf, hide_index=True, width="stretch", disabled=["Employee", "Email"], key=f"x_{month}")
    extras = {r["Email"]: {"earning": r["Additional earning (₹)"], "earning_label": r["Earning name"],
                           "deduction": r["Additional deduction (₹)"], "deduction_label": r["Deduction name"]}
              for _, r in xed.iterrows()}

    section("3.", "Calculate, check and publish")
    c1, c2, c3 = st.columns(3)
    if c1.button("Calculate Draft Payroll", width="stretch"):
        slips, problems = _compute_month(ctx, m, extras)
        pdb.save_run(month, slips, extras, me["email"])
        ctx.audit("payroll_calculated", f"{month}: {len(slips)} payslip(s)", month)
        st.session_state["run_problems"] = problems
        st.session_state["run_msg"] = f"Draft calculated for {len(slips)} employee(s). Check it below, then publish."
        st.rerun()
    for p in st.session_state.get("run_problems", []):
        st.warning(p)
    run = pdb.get_run(month)
    if run:
        slips = pdb.run_slips(run["id"])
        st.info(f"Draft calculated {ist(run['created_at'])} by {run['created_by']}. Employees cannot see it until "
                f"you publish.")
        _run_table(slips, emps)
        with c2:
            with st.popover("Publish Payslips", width="stretch"):
                st.write(f"Publish {len(slips)} payslip(s) for **{m:%B %Y}**? Employees will see them and get a "
                         f"notification. A published month cannot be changed.")
                if st.button("Yes, publish", key=f"pub_{month}"):
                    pdb.publish_run(month, me["email"])
                    ctx.audit("payroll_published", f"{month}: {len(slips)} payslip(s)", month)
                    st.session_state["run_msg"] = f"{m:%B %Y} payroll published."
                    st.session_state.pop("run_problems", None)
                    st.rerun()
        if c3.button("Discard Draft", width="stretch"):
            pdb.delete_draft(month)
            ctx.audit("payroll_discarded", month, month)
            st.session_state["run_msg"] = "Draft discarded."
            st.rerun()


def _run_table(slips, emps):
    if not slips:
        st.caption("No payslips.")
        return
    rows = []
    for s in slips:
        x, e = s["data"], emps.get(s["employee_email"], {})
        rows.append({"Employee": e.get("full_name", s["employee_email"]), "Code": e.get("emp_code", ""),
                     "Paid days": x["paid_days"], "LOP": x["lwp_days"], "Gross": x["gross"],
                     "PF": x["deductions"].get("PF (Employee)", 0), "VPF": x["deductions"].get("VPF", 0),
                     "ESI": x["deductions"].get("ESI (Employee)", 0), "PT": x["deductions"].get("Professional Tax", 0),
                     "TDS": x["deductions"].get("Income Tax (TDS)", 0),
                     "Other": x["total_deductions"] - sum(x["deductions"].get(k, 0) for k in
                                                          ("PF (Employee)", "VPF", "ESI (Employee)",
                                                           "Professional Tax", "Income Tax (TDS)")),
                     "Net pay": x["net"], "Employer PF": x["employer"].get("PF (Employer)", 0),
                     "Regime": x["regime"].title(), "Annual tax (projected)": x["tax"]["annual_tax"]})
    df = pd.DataFrame(rows).sort_values("Employee")
    st.dataframe(df, hide_index=True, width="stretch")
    tot = df[["Gross", "PF", "VPF", "ESI", "PT", "TDS", "Other", "Net pay", "Employer PF"]].sum()
    st.caption(f"{len(df)} employee(s) · Gross {inr(tot['Gross'])} · TDS {inr(tot['TDS'])} · PF (employee) "
               f"{inr(tot['PF'])} · Net pay {inr(tot['Net pay'])} · Employer PF {inr(tot['Employer PF'])}")


def tax_admin(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("taxadm_msg")
    fy = st.selectbox("Tax year", [fy_of(today_ist())] + [k for k in sorted(TAX_RULES, reverse=True)
                                                           if k != fy_of(today_ist())], key="taxadm_fy")
    emps = pdb.list_employees(include_exited=False)
    section("1.", f"Regime chosen by each employee – {fy}")
    rows = []
    for e in emps:
        c = pdb.tax_choice(e["email"], fy)
        rows.append({"Name": e["full_name"], "Code": e["emp_code"], "Regime": "New" if c["regime"] == "new" else "Old",
                     "Chosen": "Yes" if c["updated_at"] else "Not yet (new regime by default)",
                     "Last updated": ist(c["updated_at"]), "By": c["updated_by"] or ""})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    lock = bool(float(ctx.settings.get("lock_tax_choice", 0) or 0))
    new_lock = st.toggle("Lock employees' tax choices (only Finance can change them)", lock)
    if new_lock != lock:
        pdb.save_settings({"lock_tax_choice": 1 if new_lock else 0})
        ctx.audit("tax_lock_changed", "locked" if new_lock else "unlocked")
        st.rerun()
    section("2.", "Change an employee's regime or declarations")
    e, _ = _emp_pick(pdb, "taxadm_emp")
    if not e:
        return
    c = pdb.tax_choice(e["email"], fy)
    with st.form(f"taxadm_{e['email']}_{fy}"):
        regime = st.radio("Tax regime", ["New regime", "Old regime"], horizontal=True,
                          index=0 if c["regime"] == "new" else 1)
        vals = {}
        cols = st.columns(2)
        for i, (k, label) in enumerate(DECL_FIELDS):
            vals[k] = cols[i % 2].number_input(label, 0.0, 10_000_000.0, float(c["decl"].get(k) or 0), 1000.0)
        vals["metro_city"] = st.checkbox("Metro city for HRA", bool(c["decl"].get("metro_city")))
        if st.form_submit_button("Save"):
            reg = "new" if regime.startswith("New") else "old"
            pdb.save_tax_choice(e["email"], fy, reg, vals, me["email"])
            ctx.audit("tax_choice_saved", f"{e['full_name']} {fy}: {reg} regime (by Finance)", e["email"])
            pdb.notify(e["email"], f"Finance updated your tax choice for {fy} ({reg} regime).")
            st.session_state["taxadm_msg"] = f"Saved for {e['full_name']}."
            st.rerun()
    opening_section(ctx)


def opening_section(ctx):
    pdb, me = ctx.pdb, ctx.user
    section("3.", "Opening year-to-date figures")
    st.caption("Enter what was already paid and deducted in this tax year BEFORE the Portal's first payroll "
               "(from your previous payroll system), or a new joiner's salary and TDS from their previous employer "
               "(Form 12B). Without this, the Portal only sees part of the year and deducts too little tax.")
    fy = st.session_state.get("taxadm_fy") or fy_of(today_ist())
    e, _ = _emp_pick(pdb, "open_emp")
    if not e:
        return
    o = pdb.opening_ytd(e["email"], fy)
    with st.form(f"opening_{e['email']}_{fy}"):
        c1, c2, c3 = st.columns(3)
        gross = c1.number_input("Gross salary paid", 0.0, 100_000_000.0, float(o.get("gross") or 0), 1000.0)
        tds = c2.number_input("Income tax (TDS) deducted", 0.0, 100_000_000.0, float(o.get("tds") or 0), 100.0)
        pf = c3.number_input("Employee PF + VPF deducted", 0.0, 10_000_000.0, float(o.get("pf_vpf") or 0), 100.0)
        c4, c5, c6 = st.columns(3)
        pt = c4.number_input("Professional Tax deducted", 0.0, 2500.0, float(o.get("pt") or 0), 100.0)
        basic = c5.number_input("Basic paid (for old-regime HRA)", 0.0, 100_000_000.0, float(o.get("basic") or 0),
                                1000.0)
        hra = c6.number_input("HRA paid (for old-regime HRA)", 0.0, 100_000_000.0, float(o.get("hra") or 0), 1000.0)
        if st.form_submit_button(f"Save Opening Figures for {fy}"):
            pdb.save_opening_ytd(e["email"], fy, {"gross": gross, "tds": tds, "pf_vpf": pf, "pt": pt, "basic": basic,
                                                  "hra": hra}, me["email"])
            ctx.audit("opening_ytd_saved", f"{e['full_name']} {fy}", e["email"])
            st.session_state["taxadm_msg"] = f"Opening figures saved for {e['full_name']} ({fy})."
            st.rerun()
    if o.get("updated_at"):
        st.caption(f"Last saved {ist(o['updated_at'])} by {o['updated_by']}.")


def reports(ctx):
    pdb, me = ctx.pdb, ctx.user
    runs = [r for r in pdb.list_runs() if r["status"] == "Published"]
    section("1.", "Monthly reports")
    if not runs:
        st.caption("No published payroll yet.")
        return
    labels = {datetime.strptime(r["month"], "%Y-%m").strftime("%B %Y"): r for r in runs}
    pick = st.selectbox("Month", list(labels), key="rep_month")
    run = labels[pick]
    slips = pdb.run_slips(run["id"])
    emps = {e["email"]: e for e in pdb.list_employees()}
    register, bank, stat = [], [], []
    for s in slips:
        x, e = s["data"], emps.get(s["employee_email"], {})
        register.append({"Code": e.get("emp_code"), "Name": e.get("full_name"), "Department": e.get("department"),
                         "Paid days": x["paid_days"], "LOP": x["lwp_days"], **x["earnings"], "Gross": x["gross"],
                         **{f"Ded: {k}": v for k, v in x["deductions"].items()}, "Total deductions":
                         x["total_deductions"], "Net pay": x["net"], **x["employer"]})
        p = e.get("private") or {}
        bank.append({"Code": e.get("emp_code"), "Name": e.get("full_name"), "Bank": p.get("bank_name", ""),
                     "Account number": p.get("bank_account", ""), "IFSC": p.get("ifsc", ""), "Net pay": x["net"]})
        stat.append({"Code": e.get("emp_code"), "Name": e.get("full_name"), "UAN": e.get("uan") or "",
                     "PAN": p.get("pan", ""), "PF wages": min(x["earnings"].get("Basic", 0),
                                                              ctx.settings["pf_wage_ceiling"]),
                     "PF employee": x["deductions"].get("PF (Employee)", 0), "VPF": x["deductions"].get("VPF", 0),
                     "PF employer": x["employer"].get("PF (Employer)", 0),
                     "ESI employee": x["deductions"].get("ESI (Employee)", 0),
                     "ESI employer": x["employer"].get("ESI (Employer)", 0),
                     "Professional Tax": x["deductions"].get("Professional Tax", 0),
                     "TDS": x["deductions"].get("Income Tax (TDS)", 0)})
    c1, c2, c3, c4 = st.columns(4)
    c1.download_button("Payroll register (Excel)", excel_bytes({"Register": pd.DataFrame(register).fillna(0)}),
                       f"payroll_register_{run['month']}.xlsx", on_click=ctx.audit,
                       args=("report_exported", f"payroll register {run['month']}"), width="stretch")
    c2.download_button("Bank transfer sheet (Excel)", excel_bytes({"Bank transfer": pd.DataFrame(bank)}),
                       f"bank_transfer_{run['month']}.xlsx", on_click=ctx.audit,
                       args=("report_exported", f"bank transfer sheet {run['month']}"), width="stretch")
    c3.download_button("PF / ESI / PT / TDS (Excel)", excel_bytes({"Statutory": pd.DataFrame(stat)}),
                       f"statutory_{run['month']}.xlsx", on_click=ctx.audit,
                       args=("report_exported", f"statutory summary {run['month']}"), width="stretch")
    if c4.button("Prepare all payslips (ZIP)", width="stretch"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for s in slips:
                e = emps.get(s["employee_email"])
                if e:
                    z.writestr(f"Payslip_{e['emp_code']}_{run['month']}.pdf",
                               payslip_pdf(s["data"], e, ctx.settings["company_name"], ctx.settings["company_address"]))
        st.session_state["slip_zip"] = (run["month"], buf.getvalue())
        ctx.audit("payslips_exported", f"all payslips {run['month']} ({len(slips)})")
    if st.session_state.get("slip_zip", (None,))[0] == run["month"]:
        st.download_button(f"Download {len(slips)} payslips (ZIP)", st.session_state["slip_zip"][1],
                           f"payslips_{run['month']}.zip", "application/zip")
    st.dataframe(pd.DataFrame(register).fillna(0), hide_index=True, width="stretch")
    st.caption("These files contain salary, PAN and bank details. Store them securely and delete local copies "
               "after use.")

    section("2.", "One employee's payslip")
    e, _ = _emp_pick(pdb, "rep_emp", include_exited=True)
    if e:
        mine = pdb.my_payslips(e["email"])
        if mine:
            lab = {datetime.strptime(s["month"], "%Y-%m").strftime("%B %Y"): s for s in mine}
            mp = st.selectbox("Month", list(lab), key="rep_emp_month")
            slip_view(lab[mp]["data"])
            st.download_button("Download PDF", payslip_pdf(lab[mp]["data"], e, ctx.settings["company_name"],
                                                           ctx.settings["company_address"]),
                               f"Payslip_{e['emp_code']}_{lab[mp]['month']}.pdf", "application/pdf",
                               on_click=ctx.audit, args=("payslip_downloaded", f"{e['full_name']} {lab[mp]['month']}",
                                                         e["email"]))
        else:
            st.caption("No published payslips for this employee.")


def settings_admin(ctx):
    pdb, me = ctx.pdb, ctx.user
    flash("set_msg")
    s = ctx.settings
    section("1.", "Company details and statutory settings")
    st.warning("PF wage ceiling: the long-standing limit is ₹15,000 a month. Some 2026 reports say it was raised to "
               "₹25,000 by a September 2026 notification under the Code on Social Security. Please confirm the "
               "figure with your payroll adviser and set it below.")
    with st.form("fin_settings"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Company name (on payslips)", s["company_name"])
        addr = c2.text_input("Company address (on payslips)", s["company_address"])
        c3, c4, c5 = st.columns(3)
        ceiling = c3.number_input("PF wage ceiling (₹/month)", 0.0, 1_000_000.0, float(s["pf_wage_ceiling"]), 1000.0)
        rate = c4.number_input("PF rate (employee and employer)", 0.0, 0.5, float(s["pf_rate"]), 0.01)
        pt = c5.number_input("Default Professional Tax (₹/month, unknown state)", 0.0, 2500.0, float(s["default_pt"]),
                             50.0)
        c6, c7, c8 = st.columns(3)
        esi_lim = c6.number_input("ESI applies up to gross (₹/month)", 0.0, 1_000_000.0, float(s["esi_gross_limit"]),
                                  1000.0)
        esi_e = c7.number_input("ESI employee rate", 0.0, 0.1, float(s["esi_employee_rate"]), 0.0025, format="%.4f")
        esi_r = c8.number_input("ESI employer rate", 0.0, 0.1, float(s["esi_employer_rate"]), 0.0025, format="%.4f")
        if st.form_submit_button("Save Settings"):
            pdb.save_settings({"company_name": name, "company_address": addr, "pf_wage_ceiling": ceiling,
                               "pf_rate": rate, "default_pt": pt, "esi_gross_limit": esi_lim,
                               "esi_employee_rate": esi_e, "esi_employer_rate": esi_r})
            ctx.audit("finance_settings_changed", f"PF ceiling {ceiling:g}, PF rate {rate:g}, ESI limit {esi_lim:g}")
            st.session_state["set_msg"] = "Settings saved. They apply to payroll calculated from now on."
            st.rerun()
    st.caption("Income-tax slabs for 2026-27 are built in (new regime: nil up to ₹4L … 30% above ₹24L, standard "
               "deduction ₹75,000, no tax up to ₹12L taxable; old regime: standard deduction ₹50,000, rebate up to "
               "₹5L). Professional Tax defaults follow common state rules and can be overridden per employee.")
