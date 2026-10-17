"""
Resume Bank inside the Ciliconchip Portal - the frozen Resume Bank v1.7 screens, unchanged:
upload, AI / plain-English search, Match a Job (AI), similar profiles, remarks and CTC history.
"""

import io
import mimetypes
import os
import zipfile
from datetime import timedelta

import pandas as pd
import streamlit as st

from .ai import jd_text_for_embedding, profile_text, to_blob
from .db import PROFILE_STATUSES, query_words
from .extractor import ALL_SKILLS, extract_profile
from .matcher import extract_jd_skills, required_experience
from .nlq import parse_request
from .parser import extract_text

MAX_UPLOAD = 20
IST = timedelta(hours=5, minutes=30)


def ist(dt, fmt="%d %b %Y, %H:%M"):
    return (dt + IST).strftime(fmt) if dt else ""


def render(db, ai, ai_error, user, audit, IS_ADMIN, CAN_EDIT):
    """db: ResumeDB; IS_ADMIN may delete resumes; CAN_EDIT may upload and update profiles."""
    ME = user["full_name"]
    def zip_resumes(ids):
        buf, used = io.BytesIO(), set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rid, name, fname, data in db.get_files(ids):
                if not data:
                    continue
                arc = fname or f"{name}.pdf"
                while arc in used:
                    root, ext = os.path.splitext(arc)
                    arc = f"{root}_{rid}{ext}"
                used.add(arc)
                z.writestr(arc, data)
        return buf.getvalue()

    top_l, top_m = st.columns([1.3, 4.7])
    with top_l:
        st.markdown(f'<div class="metric-card" style="min-width:200px">'
                    f'<div class="metric-value">{db.count()}</div>'
                    f'<div class="metric-label">RESUMES IN THE BANK</div></div>', unsafe_allow_html=True)
    with top_m:
        if ai is not None:
            done, total = db.ai_coverage(ai.name)
            st.markdown(f'<div class="metric-card"><div class="metric-value" style="font-size:20px;color:#087A4A">'
                        f'AI active</div><div class="metric-label">LANGUAGE MODEL ALL-MINILM-L6-V2 · {done} OF {total} '
                        f'RESUMES HAVE AN AI FINGERPRINT</div></div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="metric-card"><div class="metric-value" style="font-size:20px;color:#B42318">'
                        'AI unavailable</div><div class="metric-label">KEYWORD SEARCH STILL WORKS · AI SEARCH, JOB '
                        'MATCHING AND SIMILAR PROFILES ARE OFF</div></div>', unsafe_allow_html=True)
            st.caption(f"Reason: {str(ai_error)[:200]}")

    tab_names = (["Upload Resumes"] if CAN_EDIT else []) + ["Search Resumes", "Match a Job (AI)"]
    _tabs = dict(zip(tab_names, st.tabs(tab_names)))
    upload_tab, search_tab, match_tab = (_tabs.get(n) for n in ("Upload Resumes", "Search Resumes",
                                                                 "Match a Job (AI)"))

    # ===================================================================
    # UPLOAD
    # ===================================================================
    if CAN_EDIT:
        with upload_tab:
            st.markdown(f'<div class="section-label"><span>1.</span> Upload resumes '
                        f'<span style="font-size:13px;color:#666">(PDF or Word, up to {MAX_UPLOAD} at a time)</span></div>',
                        unsafe_allow_html=True)

            st.session_state.setdefault("uploader_key", 0)
            files = st.file_uploader("Resumes", type=["pdf", "docx"], accept_multiple_files=True,
                                     label_visibility="collapsed", key=f"uploader_{st.session_state['uploader_key']}")
            st.caption(f"Resumes you save are recorded as uploaded by **{ME}**.")

            if files and len(files) > MAX_UPLOAD:
                st.error(f"Please upload no more than {MAX_UPLOAD} resumes at a time.")
                files = files[:MAX_UPLOAD]

            if st.button("Read Resumes", width="stretch", disabled=not files):
                pending = []
                seen = {}  # duplicates inside this same upload
                bar = st.progress(0.0)
                for i, f in enumerate(files, 1):
                    data = f.getvalue()
                    try:
                        text_ = extract_text(f)
                    except Exception:
                        text_ = ""
                    readable = len(text_.strip()) >= 80
                    prof = extract_profile(text_, f.name) if readable else {
                        "full_name": "", "email": "", "phone": "", "location": "", "current_role": "",
                        "primary_skills": [], "secondary_skills": [], "experience_years": None,
                        "experience_source": "", "experience_detail": "", "experience_note": "", "education": ""}
                    dup = db.find_duplicate(db.file_hash(data), prof["email"], prof["full_name"], prof["phone"])
                    keys = [k for k in (
                        "h:" + db.file_hash(data),
                        "e:" + prof["email"].lower() if prof["email"] else "",
                        "np:" + db.name_key(prof["full_name"]) + db.phone_key(prof["phone"])
                        if db.name_key(prof["full_name"]) and db.phone_key(prof["phone"]) else "",
                    ) if k]
                    twin = next((seen[k] for k in keys if k in seen), None)
                    if not readable:
                        status = "Could not read text – check file"
                    elif dup:
                        added = ist(dup["uploaded_at"], "%d %b %Y")
                        status = f"Profile already exists – {dup['full_name']}, added {added} ({dup['reason']})"
                    elif twin:
                        status = f"Profile already exists – same person as {twin} in this upload"
                    else:
                        status = "New"
                        for k in keys:
                            seen.setdefault(k, f.name)
                    pending.append({"file_name": f.name, "data": data, "text": text_, "profile": prof,
                                    "status": status, "readable": readable and status == "New"})
                    bar.progress(i / len(files))
                bar.empty()
                st.session_state["pending"] = pending

            pending = st.session_state.get("pending")
            if pending:
                st.markdown('<div class="section-label"><span>2.</span> Check the details, correct anything, then save'
                            '</div>', unsafe_allow_html=True)
                n_dup = sum(p["status"].startswith("Profile already exists") for p in pending)
                if n_dup:
                    st.error(f"Profile already exists: {n_dup} of these resumes are already in the bank and will not be "
                             "saved again. To update one, search for it and use 'Update profile' on the resume view.")
                st.caption("Every cell can be edited. Enter Current and Expected CTC in lakhs per annum (LPA). Pick a Profile status and add Remarks (e.g. 'Rejected – weak in "
                           "XCUITest', 'Referred by Ravi K'). Separate skills with commas. Untick 'Save' to skip a resume.")

                df = pd.DataFrame([{
                    "Save": p["readable"],
                    "Record": p["status"],
                    "Name": p["profile"]["full_name"],
                    "Profile status": "",
                    "Remarks": "",
                    "Email": p["profile"]["email"],
                    "Phone": p["profile"]["phone"],
                    "Location": p["profile"]["location"],
                    "Experience (yrs)": p["profile"]["experience_years"],
                    "Current CTC (LPA)": None,
                    "Expected CTC (LPA)": None,
                    "Current role": p["profile"]["current_role"],
                    "Primary skills": ", ".join(p["profile"]["primary_skills"]),
                    "Secondary skills": ", ".join(p["profile"]["secondary_skills"]),
                    "Note": p["profile"]["experience_note"],
                    "File": p["file_name"],
                } for p in pending])

                edited = st.data_editor(
                    df, hide_index=True, width="stretch", num_rows="fixed", key="review_editor",
                    disabled=["Record", "Note", "File"],
                    column_config={
                        "Save": st.column_config.CheckboxColumn(width="small"),
                        "Record": st.column_config.TextColumn(width="large"),
                        "Profile status": st.column_config.SelectboxColumn(options=[""] + PROFILE_STATUSES, width="medium"),
                        "Remarks": st.column_config.TextColumn(width="large", max_chars=1000),
                        "Current CTC (LPA)": st.column_config.NumberColumn(min_value=0.0, max_value=500.0, step=0.1,
                                                                           format="%.1f"),
                        "Expected CTC (LPA)": st.column_config.NumberColumn(min_value=0.0, max_value=500.0, step=0.1,
                                                                            format="%.1f"),
                        "Experience (yrs)": st.column_config.NumberColumn(min_value=0.0, max_value=50.0, step=0.1,
                                                                          format="%.1f"),
                        "Primary skills": st.column_config.TextColumn(width="large"),
                        "Secondary skills": st.column_config.TextColumn(width="large"),
                    },
                )

                c1, c2 = st.columns([3, 1])
                with c1:
                    save_clicked = st.button(f"Save to Database", width="stretch", key="save_btn")
                with c2:
                    if st.button("Discard", width="stretch", key="discard_btn"):
                        st.session_state.pop("pending", None)
                        st.session_state["uploader_key"] += 1
                        st.rerun()

                if save_clicked:
                    counts = {"new": 0, "duplicate": 0, "skipped": 0, "failed": 0}
                    saved_names = []
                    num = lambda v: None if v is None or pd.isna(v) else round(float(v), 1)
                    errors = []
                    split = lambda v: [s.strip() for s in str(v or "").split(",") if s.strip()]
                    for p, (_, row) in zip(pending, edited.iterrows()):
                        if p["status"].startswith("Profile already exists"):
                            counts["duplicate"] += 1
                            errors.append(p["status"])
                            continue
                        if not row["Save"]:
                            counts["skipped"] += 1
                            continue
                        exp = row["Experience (yrs)"]
                        record = {
                            **p["profile"],
                            "full_name": str(row["Name"] or "").strip(),
                            "email": str(row["Email"] or "").strip(),
                            "phone": str(row["Phone"] or "").strip(),
                            "location": str(row["Location"] or "").strip(),
                            "current_role": str(row["Current role"] or "").strip(),
                            "experience_years": None if pd.isna(exp) else round(float(exp), 1),
                            "current_ctc": num(row["Current CTC (LPA)"]),
                            "expected_ctc": num(row["Expected CTC (LPA)"]),
                            "primary_skills": split(row["Primary skills"]),
                            "secondary_skills": split(row["Secondary skills"]),
                            "notes": p["profile"].get("experience_note", ""),
                            "resume_text": p["text"],
                        }
                        if exp != p["profile"]["experience_years"]:
                            record["experience_source"] = "entered by TA"
                        try:
                            clean = lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()
                            emb = to_blob(ai.encode([profile_text(record)])[0]) if ai is not None else None
                            outcome = db.save(record, p["data"], p["file_name"], ME,
                                              status=clean(row["Profile status"]), remark=clean(row["Remarks"]),
                                              embedding=emb, embedding_model=ai.name if ai is not None else "")
                            counts[outcome] += 1
                            if outcome == "new":
                                saved_names.append(record["full_name"] or p["file_name"])
                                audit("resume_uploaded", f"{record['full_name']} ({p['file_name']})")
                            if outcome == "duplicate":
                                dup = db.find_duplicate(db.file_hash(p["data"]), record["email"], record["full_name"],
                                                        record["phone"])
                                errors.append(f"Profile already exists – {record['full_name'] or p['file_name']}"
                                              + (f" (matches {dup['full_name']}: {dup['reason']})" if dup else ""))
                                audit("duplicate_blocked", errors[-1])
                        except Exception as exc:
                            counts["failed"] += 1
                            errors.append(f"Not saved – {p['file_name']}: {str(exc)[:150]}")
                    st.session_state.pop("pending", None)
                    st.session_state.pop("search_results", None)
                    st.session_state["uploader_key"] += 1
                    st.session_state["save_message"] = (counts, errors, saved_names)
                    st.rerun()

            if "save_message" in st.session_state:
                counts, errors, saved_names = st.session_state.pop("save_message")
                if counts["new"]:
                    st.success(f"Saved {counts['new']} new profile(s) to the database: {', '.join(saved_names)}.")
                if counts["skipped"]:
                    st.info(f"{counts['skipped']} resume(s) skipped (not ticked or unreadable).")
                for e in errors:
                    st.error(e)

            st.markdown('<div class="section-label"><span>3.</span> Recently added to the bank</div>',
                        unsafe_allow_html=True)
            recent = db.recent(10)
            if recent:
                st.dataframe(pd.DataFrame([{
                    "Name": r["full_name"],
                    "Current role": r["current_role"] or "",
                    "Experience (yrs)": "" if r["experience_years"] is None else f"{r['experience_years']:g}",
                    "Profile status": r["latest_status"] or "",
                    "Added by": r["uploaded_by"] or "",
                    "Added on (IST)": ist(r["uploaded_at"]),
                    "Last updated by": r["updated_by"] or "",
                    "Last updated (IST)": ist(r["updated_at"]),
                } for r in recent]), hide_index=True, width="stretch")
                st.caption("Read live from the database. If a resume is listed here, it is saved.")
            else:
                st.caption("No resumes saved yet.")

    # ===================================================================
    # SEARCH
    # ===================================================================
    with search_tab:
        st.markdown('<div class="section-label"><span>1.</span> What are you looking for?</div>',
                    unsafe_allow_html=True)

        known = sorted(set(db.known_skills()) | set(ALL_SKILLS), key=str.lower)
        with st.form("search_form"):
            q1, q2 = st.columns([4, 1])
            with q1:
                ai_query = st.text_area(
                    "AI search – ask in plain English, or type a name, email or phone (optional)",
                    height=80,
                    placeholder="e.g. Java developers in Hyderabad with 5+ years, expected CTC under 20 LPA, not rejected"
                                "  ·  referrals from Arul this month  ·  how many AI engineers  ·  Mahesh",
                    help="Write it the way you would ask a colleague. The app picks out skills, experience "
                         "(5+ years, 3-6 yrs, under 8 years), location, CTC (under 20 LPA, current CTC below 12), "
                         "status (rejected, on hold, referred, not rejected), dates (this month, last 7 days), "
                         "'most experienced' / 'latest' ordering and 'how many' questions, and shows what it "
                         "understood. Whatever is left (a name, email, phone or description) is matched by its words "
                         "or by AI meaning.")
            with q2:
                min_ai = st.slider("Minimum AI match %", 0, 80, 30, 5, disabled=ai is None,
                                   help="For profiles found only by meaning: hide those weaker than this. Profiles that "
                                        "contain every word you typed (e.g. a name) are always shown. Good meaning "
                                        "matches usually score 40-70%; above 80% is rare.")
            s1, s2 = st.columns([4, 1])
            with s1:
                skills = st.multiselect("Skills / technologies / role", known, accept_new_options=True,
                                        placeholder="Pick from the list, or type anything (e.g. AI Engineer) and press Enter")
            with s2:
                match = st.radio("Candidates must have", ["All skills", "Any skill"])
            e1, e2, e3 = st.columns([1, 1, 2])
            with e1:
                min_exp = st.number_input("Min experience (yrs)", 0.0, 50.0, 0.0, 0.5)
            with e2:
                max_exp = st.number_input("Max experience (yrs)", 0.0, 50.0, 50.0, 0.5)
            with e3:
                location = st.text_input("Location (optional)", placeholder="e.g. Hyderabad, Bengaluru")
            c1_, c2_, c3_, c4_ = st.columns([1, 1, 1, 1.3])
            with c1_:
                min_ctc = st.number_input("Expected CTC from (LPA)", 0.0, 500.0, 0.0, 0.5, help="0 = no lower limit")
            with c2_:
                max_ctc = st.number_input("Expected CTC up to (LPA)", 0.0, 500.0, 0.0, 0.5, help="0 = no upper limit")
            with c3_:
                max_cur = st.number_input("Current CTC up to (LPA)", 0.0, 500.0, 0.0, 0.5, help="0 = no limit")
            with c4_:
                include_no_ctc = st.checkbox("Include profiles with no CTC recorded", value=True)
            k1, k2, k3 = st.columns([2, 1.3, 1])
            with k1:
                keywords = st.text_input("Other keywords (optional)",
                                         placeholder="e.g. referred by Ravi, banking — searched in names, contacts, "
                                                     "remarks and resumes",
                                         help="Every word must be found, in any order, anywhere in the profile: name, "
                                              "email, phone, status, remarks (and who added them) or resume. Different "
                                              "forms count: 'referred by Arul' also finds 'Arul's referral'. Separate "
                                              "several keywords with commas; each must match.")
            with k2:
                statuses = st.multiselect("Profile status (optional)", PROFILE_STATUSES)
            with k3:
                include_unknown = st.checkbox("Include resumes with unknown experience", value=False)
            go = st.form_submit_button("Search Resumes", width="stretch")

        if go:
            aliases = {"bangalore": "bengaluru", "gurgaon": "gurugram", "cochin": "kochi", "bombay": "mumbai",
                       "madras": "chennai", "trivandrum": "thiruvananthapuram", "vizag": "visakhapatnam"}
            loc = ",".join(aliases.get(l.strip().lower(), l.strip()) for l in location.split(",") if l.strip())
            req = parse_request(ai_query) if ai_query.strip() else {"understood": [], "words": "", "meaning": ""}
            # the request typed in the AI box and the boxes below are combined (both must hold)
            skills = list(skills)
            for s_ in req.get("skills", []):
                if s_.lower() not in {x.lower() for x in skills}:
                    skills.append(s_)
            statuses = list(dict.fromkeys(list(statuses) + req.get("statuses", [])))
            exclude = req.get("exclude_statuses") or None
            lo = [v for v in (min_exp if min_exp > 0 else None, req.get("min_exp")) if v is not None]
            hi = [v for v in (max_exp if max_exp < 50 else None, req.get("max_exp")) if v is not None]
            f_min_exp, f_max_exp = (max(lo) if lo else None), (min(hi) if hi else None)
            loc = loc or req.get("location", "")
            f_min_ctc = min_ctc if min_ctc > 0 else req.get("min_expected_ctc")
            f_max_ctc = max_ctc if max_ctc > 0 else req.get("max_expected_ctc")
            f_max_cur = max_cur if max_cur > 0 else req.get("max_current_ctc")
            after = req.get("uploaded_after")
            words, meaning = req["words"], req["meaning"]
            # A name / description (words left over) is found by its words OR by AI meaning above the slider.
            # When the request was fully turned into filters (or names a skill), every profile passing the
            # filters is shown, ranked by AI meaning - the slider does not hide any of them.
            filters_mode = bool(req.get("skills")) or not words
            qvec = ai.encode([meaning or words])[0] if (ai is not None and (meaning or words)) else None
            model = ai.name if ai is not None else ""
            ai_kw = dict(query_text="" if filters_mode else words, query_vec=qvec, model_name=model,
                         min_ai=0.0 if filters_mode else float(min_ai))
            base = dict(skills=skills, match_all=(match == "All skills"), known_skills=known,
                        min_exp=f_min_exp, max_exp=f_max_exp, include_unknown_exp=include_unknown, location=loc,
                        keywords=keywords, statuses=statuses, exclude_statuses=exclude, uploaded_after=after,
                        min_expected_ctc=f_min_ctc, max_expected_ctc=f_max_ctc, max_current_ctc=f_max_cur,
                        include_unknown_ctc=include_no_ctc)
            results = db.search(**base, **ai_kw)
            if filters_mode and qvec is not None:
                for r in results:
                    r["found_by"] = "Your request" if req["understood"] else "AI meaning"
            sort = req.get("sort")
            if sort == "exp_desc":
                results.sort(key=lambda r: (r["experience_years"] is not None, r["experience_years"] or 0), reverse=True)
            elif sort == "exp_asc":
                results.sort(key=lambda r: (r["experience_years"] is None, r["experience_years"] or 0))
            elif sort == "recent":
                results.sort(key=lambda r: r["uploaded_at"] or 0, reverse=True)

            st.session_state["search_results"] = results
            st.session_state["search_ai_stats"] = None if filters_mode else db.last_ai_stats
            st.session_state["search_ai_used"] = qvec is not None or bool(words)
            st.session_state["search_min_ai"] = min_ai
            st.session_state["search_count_q"] = bool(req.get("count_question"))
            said = list(req["understood"])
            if words and not filters_mode:
                said.append(f'profiles containing "{words}" (name, email, phone, remarks or resume)'
                            + (f" or close in meaning (AI match {min_ai}%+)" if qvec is not None else ""))
            elif qvec is not None:
                said.append(f'ranked by AI meaning of "{meaning or words}"')
            st.session_state["search_understood"] = said if ai_query.strip() else None

            # each criterion on its own, used to explain an empty result
            crit = []
            wlist = query_words(words) if not filters_mode else []
            if len(wlist) >= 2:
                for w in wlist[:5]:
                    crit.append((f'Word "{w}"', dict(query_text=w)))
            elif words and not filters_mode:
                crit.append((f'AI search "{words}"', ai_kw))
            if skills:
                crit.append(("Skills " + ", ".join(skills), dict(skills=skills, match_all=(match == "All skills"),
                                                                known_skills=known)))
            if f_min_exp is not None or f_max_exp is not None:
                crit.append((f"Experience {f_min_exp or 0:g}-{f_max_exp if f_max_exp is not None else 50:g} yrs",
                             dict(min_exp=f_min_exp, max_exp=f_max_exp, include_unknown_exp=include_unknown)))
            if loc:
                crit.append((f"Location {loc}", dict(location=loc)))
            for term in [k.strip() for k in keywords.split(",") if k.strip()]:
                crit.append((f'Keyword "{term}"', dict(keywords=term)))
            if statuses:
                crit.append(("Status " + " or ".join(statuses), dict(statuses=statuses)))
            if exclude:
                crit.append(("Leaving out " + ", ".join(exclude), dict(exclude_statuses=exclude)))
            if after is not None:
                crit.append(("Added in that period", dict(uploaded_after=after)))
            if f_min_ctc or f_max_ctc or f_max_cur:
                crit.append(("CTC limits", dict(min_expected_ctc=f_min_ctc, max_expected_ctc=f_max_ctc,
                                                max_current_ctc=f_max_cur, include_unknown_ctc=include_no_ctc)))
            breakdown = []
            if not results and len(crit) >= 2:
                for label, kw in crit:
                    found = db.search(**kw, limit=50)
                    breakdown.append((label, len(found), [f["full_name"] for f in found[:3]]))
            st.session_state["search_breakdown"] = breakdown
            audit("search", "; ".join(filter(None, [
                f'request "{ai_query.strip()}"' if ai_query.strip() else "",
                ("skills " + ", ".join(skills)) if skills else "", f"keywords {keywords}" if keywords.strip() else "",
                ("status " + ", ".join(statuses)) if statuses else "", f"location {loc}" if loc else "",
            ])) + f" -> {len(results)} result(s)")
            st.session_state.pop("zip_bytes", None)

        results = st.session_state.get("search_results")
        if results is not None:
            und = st.session_state.get("search_understood")
            if und is not None:
                st.info("Understood your request as: " + ("; ".join(und) if und else "no filters") + ".")
            if st.session_state.get("search_count_q"):
                st.success(f"Answer: {len(results)} profile(s) match"
                           + (" (200 or more)" if len(results) == 200 else "") + ".")
            st.markdown(f'<div class="section-label"><span>2.</span> {len(results)} matching resume(s)'
                        + (" (showing the 200 most recent)" if len(results) == 200 else "") + '</div>',
                        unsafe_allow_html=True)

            if not results:
                stats = st.session_state.get("search_ai_stats")
                breakdown = st.session_state.get("search_breakdown") or []
                if breakdown:
                    lines = []
                    for label, n, names in breakdown:
                        who = (" — " + ", ".join(names) + (" …" if n > 3 else "")) if n else ""
                        lines.append(f"- **{label}**: {n} profile(s){who}")
                    st.warning("No single profile meets **all** of these together. Each one on its own finds:\n\n"
                               + "\n".join(lines) + "\n\nRemove or change one of them to see those profiles.")
                elif stats and stats["passed_filters"] and not stats["above_min"]:
                    st.warning(f"{stats['passed_filters']} profile(s) passed your filters, but none reached the "
                               f"{st.session_state.get('search_min_ai')}% minimum AI match (best was "
                               f"{round(stats['best'] or 0)}%). Lower the 'Minimum AI match %' slider to see them.")
                else:
                    st.info("No resumes match. Try fewer skills, 'Any skill', a wider experience range, or remove a "
                            "filter.")
            else:
                if st.session_state.get("search_ai_used"):
                    st.caption("AI search: profiles found by name, contact details or remarks come first, then by AI "
                               "match % – how closely the meaning of each resume matches your words. 'Found by' says "
                               "why each profile is listed. All other filters still apply.")
                elif skills:
                    st.caption("Ranked by skill match: listed as a primary skill, then secondary skill, then found in the "
                               "resume text as an exact phrase, then all words found. Abbreviations such as AI / "
                               "Artificial Intelligence and ML / Machine Learning are treated as the same.")
                ai_used = st.session_state.get("search_ai_used")
                table = pd.DataFrame([{
                    "Rank": i,
                    **({"Found by": r.get("found_by") or ""} if ai_used else {}),
                    **({"AI match %": "" if (r.get("ai_match") is None or r.get("found_by") in ("Name", "Contact / remarks"))
                         else round(r["ai_match"])} if ai_used else {}),
                    "Name": r["full_name"],
                    "Profile status": r["latest_status"] or "",
                    "Latest remark": r["latest_remark"] or "",
                    "Experience (yrs)": "N/A" if r["experience_years"] is None else f"{r['experience_years']:g}",
                    "Current CTC (LPA)": "" if r["current_ctc"] is None else f"{r['current_ctc']:g}",
                    "Expected CTC (LPA)": "" if r["expected_ctc"] is None else f"{r['expected_ctc']:g}",
                    "Location": r["location"],
                    "Matched skills": ", ".join(r["matched_skills"]),
                    "Primary skills": ", ".join(r["primary_skills"]),
                    "Secondary skills": ", ".join(r["secondary_skills"]),
                    "Current role": r["current_role"],
                    "Email": r["email"],
                    "Phone": r["phone"],
                    "Added on": ist(r["uploaded_at"], "%d %b %Y"),
                    "Added by": r.get("uploaded_by") or "",
                    "Last updated by": r.get("updated_by") or "",
                    "Last updated (IST)": ist(r["updated_at"]),
                    "File": r["file_name"],
                } for i, r in enumerate(results, 1)])
                st.dataframe(table, hide_index=True, width="stretch")

                d1, d2, _ = st.columns([1.3, 1, 2])
                with d1:
                    if "zip_bytes" not in st.session_state:
                        if st.button(f"Prepare all {len(results)} resumes for download", width="stretch"):
                            st.session_state["zip_bytes"] = zip_resumes([r["id"] for r in results])
                            audit("resumes_downloaded", f"{len(results)} resume(s) as ZIP from search: "
                                  + ", ".join(r["full_name"] for r in results[:50]))
                            st.rerun()
                    else:
                        st.download_button(f"Download {len(results)} resumes (ZIP)", st.session_state["zip_bytes"],
                                           "ciliconchip_matching_resumes.zip", "application/zip", width="stretch")
                with d2:
                    xbuf = io.BytesIO()
                    table.to_excel(xbuf, index=False, sheet_name="Search results")
                    st.download_button("Download list (Excel)", xbuf.getvalue(), "ciliconchip_resume_search.xlsx",
                                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                       width="stretch", on_click=audit,
                                       args=("list_exported", f"Search results list, {len(results)} profile(s)"))

                st.markdown('<div class="section-label"><span>3.</span> Open a resume</div>', unsafe_allow_html=True)
                labels = [f"{i}. {r['full_name']} — {r['file_name']}" for i, r in enumerate(results, 1)]
                pick = st.selectbox("Candidate", ["—"] + labels, label_visibility="collapsed")
                if pick != "—":
                    r = results[labels.index(pick)]
                    if st.session_state.get("last_viewed") != r["id"]:
                        st.session_state["last_viewed"] = r["id"]
                        audit("resume_viewed", r["full_name"], r["id"])
                    left, right = st.columns([2, 1])
                    with left:
                        st.markdown(f"### {r['full_name']}")
                        if r["latest_status"]:
                            st.markdown(f'<span class="pill">{r["latest_status"]}</span>', unsafe_allow_html=True)
                        st.markdown(
                            f"**Role:** {r['current_role'] or '—'}  |  **Location:** {r['location'] or '—'}  |  "
                            f"**Experience:** {'—' if r['experience_years'] is None else str(r['experience_years']) + ' yrs'}"
                            f"  |  **Education:** {r['education'] or '—'}")
                        st.markdown(f"**Email:** {r['email'] or '—'}  |  **Phone:** {r['phone'] or '—'}")
                        ctc = lambda v: "—" if v is None else f"{v:g} LPA"
                        st.markdown(f"**Current CTC:** {ctc(r['current_ctc'])}  |  "
                                    f"**Expected CTC:** {ctc(r['expected_ctc'])}")
                        if r["experience_detail"]:
                            st.caption(f"Employment periods found: {r['experience_detail']}")
                        st.caption(f"Added by {r.get('uploaded_by') or 'unknown'} on {ist(r['uploaded_at'])} IST"
                                   + (f"  ·  Last updated by {r.get('updated_by') or 'unknown'} on "
                                      f"{ist(r['updated_at'])} IST" if r.get("updated_at") else ""))
                        st.markdown("**Primary skills**")
                        st.markdown(" ".join(f'<span class="pill">{s}</span>' for s in r["primary_skills"]) or "—",
                                    unsafe_allow_html=True)
                        st.markdown("**Secondary skills**")
                        st.markdown(" ".join(f'<span class="pill sec">{s}</span>' for s in r["secondary_skills"])
                                    or "—", unsafe_allow_html=True)
                    st.markdown('<div class="section-label"><span>4.</span> Remarks history</div>',
                                unsafe_allow_html=True)
                    history = db.remark_history(r["id"])
                    if history:
                        st.dataframe(pd.DataFrame([{
                            "Date (IST)": ist(h["added_at"]),
                            "Status": h["status"] or "",
                            "Remark": h["remark"] or "",
                            "Added by": h["added_by"] or "",
                        } for h in history]), hide_index=True, width="stretch")
                    else:
                        st.caption("No remarks recorded yet for this profile.")

                    if CAN_EDIT:
                        with st.form(f"update_profile_{r['id']}", clear_on_submit=True):
                            st.markdown(f"**Update profile** – add a remark, change the status or record new CTC figures "
                                        f"(recorded as {ME})")
                            new_status = st.selectbox("Profile status", ["(no change)"] + PROFILE_STATUSES)
                            b1, b2 = st.columns([1, 1])
                            with b1:
                                new_cur = st.number_input("Current CTC (LPA)", 0.0, 500.0, value=r["current_ctc"], step=0.1,
                                                          placeholder="Not recorded")
                            with b2:
                                new_exp = st.number_input("Expected CTC (LPA)", 0.0, 500.0, value=r["expected_ctc"],
                                                          step=0.1, placeholder="Not recorded")
                            new_remark = st.text_area("Remark", max_chars=1000,
                                                      placeholder="e.g. Cleared L1, L2 scheduled for 15 Oct")
                            if st.form_submit_button("Save Update", width="stretch"):
                                status_val = "" if new_status == "(no change)" else new_status
                                cur = None if new_cur is None else round(float(new_cur), 1)
                                exp_ = None if new_exp is None else round(float(new_exp), 1)
                                changed_ctc = cur != r["current_ctc"] or exp_ != r["expected_ctc"]
                                if not status_val and not new_remark.strip() and not changed_ctc:
                                    st.warning("Nothing to save – change the status or CTC, or write a remark.")
                                else:
                                    db.update_profile(r["id"], status_val, new_remark, ME, cur, exp_,
                                                      r["current_ctc"], r["expected_ctc"])
                                    audit("profile_updated", f"{r['full_name']}: " + "; ".join(filter(None, [
                                        f"status {status_val}" if status_val else "",
                                        f"remark \"{new_remark.strip()}\"" if new_remark.strip() else "",
                                        f"CTC current {cur} / expected {exp_}" if changed_ctc else ""])), r["id"])
                                    r["updated_by"] = ME
                                    if status_val:
                                        r["latest_status"] = status_val
                                    r["current_ctc"], r["expected_ctc"] = cur, exp_
                                    if new_remark.strip():
                                        r["latest_remark"] = new_remark.strip()
                                    st.session_state["remark_saved"] = r["full_name"]
                                    st.rerun()
                    if st.session_state.get("remark_saved") == r["full_name"]:
                        st.success("Profile updated.")
                        st.session_state.pop("remark_saved")

                    with right:
                        fname, data = db.get_file(r["id"])
                        if data:
                            st.download_button("Download resume", data, fname,
                                               mimetypes.guess_type(fname)[0] or "application/octet-stream",
                                               width="stretch", key=f"dl_{r['id']}", on_click=audit,
                                               args=("resume_downloaded", f"{r['full_name']} ({fname})", r["id"]))
                        if IS_ADMIN:
                            with st.popover("Delete from bank", width="stretch"):
                                st.write(f"Permanently delete **{r['full_name']}**? This cannot be undone.")
                                if st.button("Yes, delete", key=f"del_{r['id']}"):
                                    db.delete(r["id"])
                                    audit("resume_deleted", f"{r['full_name']} ({r['file_name']})", r["id"])
                                    st.session_state.pop("search_results", None)
                                    st.rerun()
                        if ai is not None and st.button("Find similar profiles (AI)", width="stretch",
                                                        key=f"sim_{r['id']}"):
                            st.session_state["similar_for"] = (r["id"], db.similar(r["id"], ai.name, k=5))

                    sim = st.session_state.get("similar_for")
                    if sim and sim[0] == r["id"]:
                        st.markdown(f'<div class="section-label"><span>5.</span> Profiles most similar to '
                                    f'{r["full_name"]} (AI)</div>', unsafe_allow_html=True)
                        if sim[1]:
                            st.dataframe(pd.DataFrame([{
                                "AI similarity %": round(x["ai_similarity"]),
                                "Name": x["full_name"],
                                "Current role": x["current_role"] or "",
                                "Experience (yrs)": "" if x["experience_years"] is None else f"{x['experience_years']:g}",
                                "Primary skills": ", ".join(x["primary_skills"][:6]),
                                "Profile status": x["latest_status"] or "",
                                "Expected CTC (LPA)": "" if x["expected_ctc"] is None else f"{x['expected_ctc']:g}",
                                "Location": x["location"] or "",
                            } for x in sim[1]]), hide_index=True, width="stretch")
                            st.caption("Found by comparing AI fingerprints – useful when a candidate declines and you "
                                       "need alternatives with a similar background.")
                        else:
                            st.caption("No other profiles to compare with yet.")

    # ===================================================================
    # MATCH A JOB (AI)
    # ===================================================================
    with match_tab:
        st.markdown('<div class="section-label"><span>1.</span> Give the AI a job description</div>',
                    unsafe_allow_html=True)
        if ai is None:
            st.warning("The AI model is not available right now, so job matching is switched off. "
                       "Keyword search on the Search tab still works.")
        else:
            st.caption("The AI reads the job description and ranks everyone in the bank by fit – meaning match, the "
                       "JD's skills found in each resume, and experience against the requirement.")
            m1, m2 = st.columns([1, 1])
            with m1:
                jd_file = st.file_uploader("Upload the JD (PDF, Word or text)", type=["pdf", "docx", "txt"],
                                           key="jd_upload")
            with m2:
                jd_paste = st.text_area("…or paste it here", height=140, key="jd_paste",
                                        placeholder="Role, responsibilities, must-have skills, years of experience")
            st.markdown("**Who counts as a match** – profiles that miss any of these are not shown")
            g1, g2, g3 = st.columns([1, 1, 1])
            with g1:
                skill_rule = st.selectbox("JD skills the profile must have",
                                          ["All of them", "At least 75%", "At least 60% (same as screening app)",
                                           "At least half", "At least one", "No skill check"],
                                          index=2)
            with g2:
                exp_rule = st.selectbox("Experience vs what the JD asks",
                                        ["Must meet it fully", "Within 25% (e.g. 3+ yrs for 4 yrs)",
                                         "Within 50%", "No experience check"], index=1)
            with g3:
                min_fit = st.slider("Minimum overall fit %", 0, 100, 60, 5)
            o1, o2 = st.columns([1, 1])
            with o1:
                hide_rejected = st.checkbox("Leave out profiles marked Rejected", value=True)
            with o2:
                top_n = st.slider("Show at most", 5, 50, 20, 5, help="Upper limit on how many qualifying profiles to list.")
            if st.button("Find Best Matches", width="stretch", key="match_btn"):
                jd_text = ""
                if jd_file is not None:
                    try:
                        jd_text = extract_text(jd_file)
                    except Exception:
                        jd_text = ""
                jd_text = (jd_text or "").strip() or jd_paste.strip()
                if len(jd_text) < 40:
                    st.warning("Please upload or paste a job description first (at least a few lines).")
                else:
                    jd_skill_objs, _ = extract_jd_skills(jd_text)
                    jd_skills = [x["name"] for x in jd_skill_objs]
                    req = required_experience(jd_text)
                    jd_vec = ai.encode([jd_text_for_embedding(jd_text)])[0]
                    skill_ratio = {"All of them": 1.0, "At least 75%": 0.75, "At least 60% (same as screening app)": 0.6,
                                   "At least half": 0.5, "At least one": 0.01, "No skill check": 0.0}[skill_rule]
                    exp_ratio = {"Must meet it fully": 1.0, "Within 25% (e.g. 3+ yrs for 4 yrs)": 0.75,
                                 "Within 50%": 0.5, "No experience check": None}[exp_rule]
                    rows, stats = db.match_jd(jd_vec, jd_skills, req, ai.name,
                                              exclude_statuses=["Rejected"] if hide_rejected else None, top=top_n,
                                              min_fit=float(min_fit), min_skill_ratio=skill_ratio,
                                              min_exp_ratio=exp_ratio)
                    st.session_state["jd_match"] = {"skills": jd_skills, "req": req, "rows": rows, "stats": stats}
                    audit("job_matched", f"JD skills {', '.join(jd_skills) or 'none'}; {len(rows)} qualifying profile(s)")
                    st.session_state.pop("jd_zip", None)

            jm = st.session_state.get("jd_match")
            if jm:
                st.markdown('<div class="section-label"><span>2.</span> What the AI read from the JD</div>',
                            unsafe_allow_html=True)
                st.markdown("**Skills detected:** " + (", ".join(jm["skills"]) or "none listed") +
                            f"  |  **Experience asked:** {(format(jm['req'], 'g') + '+ years') if jm['req'] else 'not stated'}")
                stats = jm.get("stats") or {"checked": 0, "qualified": len(jm["rows"])}
                st.markdown(f'<div class="section-label"><span>3.</span> Matching profiles '
                            f'({len(jm["rows"])})</div>', unsafe_allow_html=True)
                hidden = stats["checked"] - stats["qualified"]
                if stats["checked"]:
                    st.caption(f"{stats['checked']} profile(s) in the bank were checked; {stats['qualified']} qualify"
                               + (f" and {hidden} did not meet the criteria above, so they are not shown." if hidden
                                  else ".")
                               + (f" Showing the top {len(jm['rows'])}." if stats["qualified"] > len(jm["rows"]) else ""))
                if not stats["checked"]:
                    st.info("No resumes with an AI fingerprint yet. Upload some resumes first.")
                elif not jm["rows"]:
                    st.info("No profile in the bank meets these criteria. Loosen the skill or experience rule, or lower "
                            "the minimum overall fit, to see the closest profiles.")
                else:
                    st.caption("Overall fit = 50% AI meaning match + 35% JD skills found + 15% experience fit "
                               "(re-balanced when the JD lists no skills or no experience).")
                    mt = pd.DataFrame([{
                        "Rank": i,
                        "Overall fit %": round(d["overall_fit"]),
                        "AI meaning match %": round(d["ai_match"]),
                        "Name": d["full_name"],
                        "Skills matched": f"{len(d['skills_matched'])} of {len(jm['skills'])}" if jm["skills"] else "—",
                        "Matched skills": ", ".join(d["skills_matched"]),
                        "Missing skills": ", ".join(d["skills_missing"]),
                        "Experience (yrs)": "" if d["experience_years"] is None else f"{d['experience_years']:g}",
                        "Current role": d["current_role"] or "",
                        "Profile status": d["latest_status"] or "",
                        "Latest remark": d["latest_remark"] or "",
                        "Expected CTC (LPA)": "" if d["expected_ctc"] is None else f"{d['expected_ctc']:g}",
                        "Location": d["location"] or "",
                        "Email": d["email"] or "",
                        "Phone": d["phone"] or "",
                    } for i, d in enumerate(jm["rows"], 1)])
                    st.dataframe(mt, hide_index=True, width="stretch")
                    z1, z2, _ = st.columns([1.3, 1, 2])
                    with z1:
                        if "jd_zip" not in st.session_state:
                            if st.button(f"Prepare top {len(jm['rows'])} resumes for download", width="stretch",
                                         key="jd_zip_btn"):
                                st.session_state["jd_zip"] = zip_resumes([d["id"] for d in jm["rows"]])
                                audit("resumes_downloaded", f"{len(jm['rows'])} resume(s) as ZIP from job match: "
                                      + ", ".join(d["full_name"] for d in jm["rows"]))
                                st.rerun()
                        else:
                            st.download_button(f"Download top {len(jm['rows'])} resumes (ZIP)", st.session_state["jd_zip"],
                                               "ciliconchip_jd_matches.zip", "application/zip", width="stretch")
                    with z2:
                        xb = io.BytesIO()
                        mt.to_excel(xb, index=False, sheet_name="JD matches")
                        st.download_button("Download list (Excel)", xb.getvalue(), "ciliconchip_jd_matches.xlsx",
                                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                           width="stretch", key="jd_xlsx", on_click=audit,
                                           args=("list_exported", f"Job match list, {len(jm['rows'])} profile(s)"))

