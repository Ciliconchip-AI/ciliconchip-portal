# Ciliconchip Portal (Powered by AI COE)

One sign-in for HR, Finance and Talent Acquisition, built for a company of about 100 people.
Built on the frozen **Resume Bank v1.7** (all its screens are included unchanged) plus two new modules, HR and Finance.

## Who sees what
Everyone with a sign-in is an **employee (Resource)**: their own leave, policies, payslips and tax choice.
**Managers** are recognised automatically from the "hardline manager" field, with no role needed. They approve their
team's leave and see the team's leave calendar and balances, but **never salaries**.

| Extra role | Adds |
|---|---|
| Super Admin | Admin tab: sign-ins, roles, password resets, audit log; full Resume Bank access |
| HR Admin | Employees (add / edit / exit / bulk Excel upload), all leave requests, balance corrections, policies, holidays, leave rules |
| Finance Admin | Salary structures, tax regimes, opening year-to-date figures, payroll runs, payslips and reports |
| Recruiter | Resume Bank: upload, search, match jobs, update candidate profiles |
| TA Viewer | Resume Bank: search, match, view and download |

HR Admin does not see salaries unless also given Finance Admin. Every change of role is logged.

## HR
- **Policies:** HR publishes PDF or Word policies, and new versions replace the old ones while history is kept. Employees open them and click
  "I have read this"; HR sees who has and hasn't acknowledged each one.
- **Leave balances:** these are calculated from the rules, never credited by a nightly job, so they cannot drift:
  - Credited on the 1st of each month of service. The joining month counts if the person joined on or before the 15th.
  - Starting rules (HR can change them in *Leave Rules & Holidays*):
    - CL 1 a month, lapses on 31 Dec.
    - PL 1.25 a month, carries forward up to 30 days.
    - SL 0.5 a month (6 a year), lapses on 31 Dec.
    - LWP is unpaid with no limit and reduces pay.
  - Leave days skip Saturdays, Sundays and company holidays. A single day can be a half day.
- **Apply and approve:**
  - The employee applies; the hardline manager approves or rejects with a comment, and the balance updates at once.
  - The app blocks overlapping requests, requests above the available balance, and approving your own leave.
  - If no manager is set, or the manager is away, HR can decide. Employees can cancel pending requests and future approved ones; HR can cancel any.
- **HR Admin:**
  - all requests, with an Excel export
  - a balance report for everyone
  - balance adjustments: opening balances when you start using the Portal, comp-offs, corrections
  - a holiday calendar for each year
- **Notifications** appear in the app (Home tab) for leave applied, decided or cancelled, policies published and
  payslips ready.

## Finance
- **Salary structure** for each employee, effective from a date, with history kept:
  - Earnings: Basic, HRA, Special Allowance, Conveyance, LTA, Other Allowance.
  - PF (capped at the PF wage ceiling, on full basic, or not applicable), VPF, Professional Tax (blank means the state rule), a recurring
    deduction, and an optional fixed TDS.
  - Bulk Excel upload is available.
- **Tax regime:** each employee chooses new or old, plus declarations (80C, 80D, NPS, home-loan interest, rent for
  HRA), and sees an estimate under both regimes. Finance can edit choices or lock them.
- **Opening year-to-date figures:** when the Portal starts mid-year, enter the pay and TDS already handled for
  April to the month before the first Portal payroll, or a new joiner's previous-employer figures (Form 12B). Without
  these, TDS is under-deducted.
- **Monthly payroll:**
  1. Calculate a draft. It applies loss of pay from approved LWP and pro-rates joiners and leavers.
  2. Add one-off earnings or deductions.
  3. Check the register, then publish.
  - Employees are notified and download their **payslip PDF** (logo top right, masked PAN and bank, net pay in words).
  - Published months are locked.
- **TDS** is projected over the full year, minus tax already deducted, then spread over the remaining months. It uses tax-year
  **2026-27** rules under the Income-tax Act 2025, the same slabs as 2025-26:
  - New regime: nil up to ₹4L, then 5/10/15/20/25%, and 30% above ₹24L. Standard deduction ₹75,000, and no tax up to ₹12L taxable, with marginal relief.
  - Old regime: standard deduction ₹50,000; rebate up to ₹5L.
  - Surcharge with marginal relief, plus 4% cess.
- **Reports:** payroll register, bank transfer sheet, PF/ESI/PT/TDS summary, and all payslips as a ZIP.
- **Settings:** company name and address, PF wage ceiling and rate, ESI limit and rates, default PT.
  **Please confirm the PF wage ceiling.** It has long been ₹15,000; some reports say a September 2026 notification
  raised it to ₹25,000.

Payroll here is a calculation aid: Finance should review each draft, and a CA should confirm the set-up once. The Portal
does not produce Form 16, Form 24Q or the PF ECR file; the reports give Finance the figures for them.

## Security and privacy
- Personal sign-ins only. Passwords are stored as salted PBKDF2 hashes, five wrong attempts lock the account for 15 minutes,
  temporary passwords must be changed at first sign-in, and you're signed out after 30 minutes idle.
- **Encrypted at rest** with a key that lives only in Streamlit secrets: salary structures, payslips, tax
  declarations, opening year-to-date figures, PAN and bank details. A copy of the database alone reveals none of them.
- Access is checked on the server for every screen; employees can only ever load their own leave and payslips.
- **Audit log** (Admin tab): sign-ins and failed attempts, every HR, Finance and Resume Bank action, payslip
  downloads and report exports. Salary amounts are never written to the log.
- Removing someone's access, or entering an exit date that has passed, takes effect on their next click.

## Performance
Streamlit's servers (US) and the Aiven database (Bengaluru) are far apart, so the Portal keeps the small tables in
memory, holding encrypted data only, and checks for changes with one tiny query per click. Tested with 100 employees
and 350 leave requests:
- an employee's or manager's click needs 1 database round trip
- payroll for 99 people calculates in under a second
- the balance report for everyone takes 0.03 seconds

## One-time setup
1. **Database:** in the Aiven console, open your MySQL service, go to **Databases**, add a database named
   `ciliconchip_portal`. This uses the same free service as the Resume Bank, with separate tables.
2. **GitHub:** create a private repository `ciliconchip-portal` and upload these files, without any `secrets.toml`.
3. **Streamlit:** **Create app** → the new repository → `app.py`. Under **Advanced settings → Secrets**, paste
   `secrets.toml.example` with your values. Set **Sharing** to public; the Portal has its own sign-in.
4. Open the app → **First-time setup**: enter `APP_PASSWORD`, your name, email and password. You become Super Admin,
   plus HR, Finance and Recruiter if you tick them.
5. If `PORTAL_ENCRYPTION_KEY` was not set, the app shows a ready-made key. Add it to Secrets and **keep a copy
   offline**.
6. **HR:** add employees (or bulk upload), set managers, enter opening leave balances, publish holidays and
   policies.
7. **Finance:** check Finance Settings, enter salary structures (or bulk upload) and opening year-to-date figures, then run
   the first payroll.

## Free-plan limits
- Aiven's free MySQL switches off when idle, and Streamlit apps sleep after a period without visitors. The first person in
  may need to wait a minute, or the admin may need to power the database on in the Aiven console.
- There are no guaranteed backups on the free plan. Download the payroll register and leave reports each month, and consider
  a paid database before you rely on it for statutory records.
- Notifications appear in the app only; there are no emails yet.

## Files
- `app.py`: sign-in, Home, navigation
- `src/ui_hr.py`, `src/ui_finance.py`, `src/ui_admin.py`, `src/ui_resume_bank.py`: the screens
- `src/portal_db.py`: database, roles, leave workflow, payroll storage, cache
- `src/leave.py`: leave rules (accrual, carry-forward, working days)
- `src/payroll.py`: tax and payroll calculation; `src/payslip.py`: payslip PDF
- `src/security.py`: encryption
- `src/db.py`, `src/ai.py`, `src/nlq.py`, `src/extractor.py`, `src/parser.py`, `src/matcher.py`: Resume Bank (from v1.7)
