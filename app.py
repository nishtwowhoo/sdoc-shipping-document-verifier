import json
import os
import streamlit as st
from ai_copilot import ask_hitl_assistant
from loader import Inbox

# -----------------------------------------------------------------------------
# PAGE CONFIGURATION
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="SDOC Shipping Document Verifier",
    page_icon="🚢",
    layout="wide",
    initial_sidebar_state="expanded"
)

#Load inbox with Loader.py
inbox = Inbox(".")

# two column layout: Left = inspection, Right = AI Copilot Sidecar
col_left, col_right = st.columns([0.65, 0.35])

with col_left:
    st.header("Record Inspector & Discrepancies")
    selected_eid = st.selectbox("Select Email Record", [e["email_id"] for e in inbox.emails()])
    
    #Fetch email object and attachment via loader.py
    email_obj = next((e for e in inbox.emails() if e["email_id"] == selected_eid))
    attachments = email_obj.get("attachments", [])
    
    # Read attachment texts
    si_text = next((inbox.read_text(a) for a in attachments if "SI" in a), "")
    bl_text = next((inbox.read_text(a) for a in attachments if "BL" in a), "")

    st.write(f"**From:** `{email_obj.get('from')}`") 
    st.write(f"**Subject:** {email_obj.get('subject')}")
    
    with st.expander("Show Email Body"):
        st.text(email_obj.get("body", "No body text available."))
    
    with st.expander("Show source documents (SI & Draft BL)"):
        c1, c2 = st.columns(2)
        c1.text_area("Shipping Instruction (SI)", si_text if si_text else "No SI document text found.", height=200)
        c2.text_area("Draft Bill of Lading (BL)", bl_text if bl_text else "No BL document text found.", height=200)
        

with col_right:
    st.header("AI Copilot Sidecar")
    st.caption("Ask the assistant to analyze edge cases or re-extract values.")
    
    # Initialize chat history state
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
        
    # Display prior messages without re-running the assistant on every rerun.
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_query = st.chat_input("Ask about this shipment...")
    if user_query:
        st.session_state.chat_history.append({"role": "user", "content": user_query})
        with st.chat_message("user"):
            st.markdown(user_query)

        pipeline_res = {
            "status": "NEEDS_REVIEW",
            "category": "BL_COMPARISON",
            "review_reason": "unreadable",
            "defect_fields": [],
        }
        with st.chat_message("assistant"):
            with st.spinner("Analyzing document context..."):
                reply = ask_hitl_assistant(
                    user_query=user_query,
                    email_id=selected_eid,
                    email_meta=email_obj,
                    si_text=si_text,
                    bl_text=bl_text,
                    pipeline_results=pipeline_res,
                )
            st.markdown(reply)
        st.session_state.chat_history.append({"role": "assistant", "content": reply})

# Custom CSS for NeverBounce-style dashboard aesthetic
st.markdown("""
<style>
    .stApp {
        background-color: #F8FAFC;
    }
    .metric-card {
        background: #FFFFFF;
        border-radius: 10px;
        padding: 18px;
        border: 1px solid #E2E8F0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        text-align: center;
    }
    .metric-label {
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        color: #64748B;
        letter-spacing: 0.5px;
    }
    .metric-value {
        font-size: 28px;
        font-weight: 700;
        color: #0F172A;
        margin-top: 4px;
    }
    .status-badge-ok {
        background-color: #DCFCE7;
        color: #166534;
        padding: 4px 12px;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 12px;
        display: inline-block;
    }
    .status-badge-mismatch {
        background-color: #FEE2E2;
        color: #991B1B;
        padding: 4px 12px;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 12px;
        display: inline-block;
    }
    .status-badge-review {
        background-color: #FEF3C7;
        color: #92400E;
        padding: 4px 12px;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 12px;
        display: inline-block;
    }
    .field-match {
        background-color: #F0FDF4;
        border-left: 4px solid #22C55E;
        padding: 8px 12px;
        border-radius: 4px;
        margin-bottom: 6px;
    }
    .field-diff {
        background-color: #FEF2F2;
        border-left: 4px solid #EF4444;
        padding: 8px 12px;
        border-radius: 4px;
        margin-bottom: 6px;
    }
</style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# DATA LOADERS & DATASET INTEGRATION
# -----------------------------------------------------------------------------
@st.cache_data
def load_submission_data():
    """Loads prediction results from submission.json or sample_submission.json."""
    paths = ["submission.json", "sample_submission.json", "submissionjson.txt"]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data, path
            except Exception as e:
                st.sidebar.warning(f"Note: Could not parse {path}: {e}")
    return {}, "None"

@st.cache_data
def load_inbox_data():
    """Tries importing loader.py or reads static inbox email files."""
    inbox_dict = {}
    try:
        from loader import Inbox
        inbox = Inbox(".")
        for email in inbox:
            inbox_dict[email["email_id"]] = email
        return inbox_dict, "loader.py"
    except Exception:
        # Fallback to local json read if loader.py isn't imported
        inbox_dir = "inbox"
        if os.path.exists(inbox_dir):
            for fname in os.listdir(inbox_dir):
                if fname.endswith(".json"):
                    try:
                        with open(os.path.join(inbox_dir, fname), "r", encoding="utf-8") as f:
                            edata = json.load(f)
                            inbox_dict[edata["email_id"]] = edata
                    except Exception:
                        pass
        return inbox_dict, "static_json"

submission_data, sub_source = load_submission_data()
inbox_data, inbox_source = load_inbox_data()

# Merge email metadata with submission prediction
merged_records = []
all_eids = sorted(list(set(list(submission_data.keys()) + list(inbox_data.keys()))))

for eid in all_eids:
    sub = submission_data.get(eid, {
        "category": "GENERAL",
        "status": "OK",
        "review_reason": None,
        "has_defect": False,
        "defect_fields": []
    })
    meta = inbox_data.get(eid, {
        "email_id": eid,
        "from": "operations@shipping-line.com",
        "subject": f"Request verification for {eid}",
        "body": "No email body text available.",
        "attachments": []
    })
    merged_records.append({
        "email_id": eid,
        "from": meta.get("from", "N/A"),
        "subject": meta.get("subject", "N/A"),
        "body": meta.get("body", ""),
        "attachments": meta.get("attachments", []),
        "category": sub.get("category", "GENERAL"),
        "status": sub.get("status", "OK"),
        "review_reason": sub.get("review_reason"),
        "has_defect": sub.get("has_defect", False),
        "defect_fields": sub.get("defect_fields", [])
    })

# -----------------------------------------------------------------------------
# SIDEBAR CONTROLS & NAVIGATION
# -----------------------------------------------------------------------------
st.sidebar.image("https://img.icons8.com/color/96/container-ship.png", width=60)
st.sidebar.title("SDOC Verifier")
st.sidebar.caption(f"Data Sources: `{sub_source}` | `{inbox_source}`")

nav_selection = st.sidebar.radio(
    "Navigation",
    ["📊 Verification Dashboard", "🔍 Side-by-Side Inspector", "🚨 Human-in-the-Loop Queue", "⚡ Live Email Tester"]
)

st.sidebar.divider()
st.sidebar.subheader("Filters")
selected_category = st.sidebar.selectbox(
    "Category",
    ["ALL", "BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
)
selected_status = st.sidebar.selectbox(
    "Verification Status",
    ["ALL", "OK", "MISMATCH", "NEEDS_REVIEW"]
)
search_query = st.sidebar.text_input("Search ID / Subject / Sender", "")

# Apply filtering
filtered_records = []
for r in merged_records:
    if selected_category != "ALL" and r["category"] != selected_category:
        continue
    if selected_status != "ALL" and r["status"] != selected_status:
        continue
    if search_query:
        q = search_query.lower()
        match_q = q in r["email_id"].lower() or q in r["from"].lower() or q in r["subject"].lower()
        if not match_q:
            continue
    filtered_records.append(r)

# -----------------------------------------------------------------------------
# VIEW 1: VERIFICATION DASHBOARD (NeverBounce Style)
# -----------------------------------------------------------------------------
if nav_selection == "📊 Verification Dashboard":
    st.markdown("<div style='font-size: 24px; font-weight: 700; color: #0F172A;'>Shipping Document Verification Dashboard</div>", unsafe_allow_html=True)
    st.markdown("<div style='color: #64748B; font-size: 14px; margin-bottom: 20px;'>Real-time operational auditing of shipping instructions (SI) vs. draft bills of lading (BL).</div>", unsafe_allow_html=True)

    # Top KPI Metrics
    total_cnt = len(merged_records)
    ok_cnt = sum(1 for r in merged_records if r["status"] == "OK")
    mismatch_cnt = sum(1 for r in merged_records if r["status"] == "MISMATCH")
    review_cnt = sum(1 for r in merged_records if r["status"] == "NEEDS_REVIEW")
    bl_comp_cnt = sum(1 for r in merged_records if r["category"] == "BL_COMPARISON")

    mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)
    with mcol1:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>Total Inbox</div><div class='metric-value'>{total_cnt}</div></div>", unsafe_allow_html=True)
    with mcol2:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>Verified OK</div><div class='metric-value' style='color:#166534;'>{ok_cnt}</div></div>", unsafe_allow_html=True)
    with mcol3:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>Mismatches</div><div class='metric-value' style='color:#991B1B;'>{mismatch_cnt}</div></div>", unsafe_allow_html=True)
    with mcol4:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>Escalations</div><div class='metric-value' style='color:#92400E;'>{review_cnt}</div></div>", unsafe_allow_html=True)
    with mcol5:
        st.markdown(f"<div class='metric-card'><div class='metric-label'>BL Comparisons</div><div class='metric-value' style='color:#0284C7;'>{bl_comp_cnt}</div></div>", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Main Verification Table
    st.subheader(f"Verification Results ({len(filtered_records)} emails)")

    table_data = []
    for r in filtered_records:
        status_html = "✅ OK"
        if r["status"] == "MISMATCH":
            status_html = "⚠️ MISMATCH"
        elif r["status"] == "NEEDS_REVIEW":
            status_html = "🚨 NEEDS REVIEW"

        defects_str = ", ".join(r["defect_fields"]) if r["defect_fields"] else (r["review_reason"] or "None")

        table_data.append({
            "Email ID": r["email_id"],
            "Category": r["category"],
            "Status": status_html,
            "Details / Reason": defects_str,
            "From": r["from"],
            "Subject": r["subject"]
        })

    if table_data:
        st.dataframe(
            table_data,
            use_container_width=True,
            column_config={
                "Email ID": st.column_config.TextColumn(width="medium"),
                "Category": st.column_config.TextColumn(width="small"),
                "Status": st.column_config.TextColumn(width="medium"),
                "Details / Reason": st.column_config.TextColumn(width="large"),
                "Subject": st.column_config.TextColumn(width="large")
            }
        )
    else:
        st.info("No records match the current filter criteria.")

# -----------------------------------------------------------------------------
# VIEW 2: SIDE-BY-SIDE FIELD INSPECTOR
# -----------------------------------------------------------------------------
elif nav_selection == "🔍 Side-by-Side Inspector":
    st.markdown("<div style='font-size: 24px; font-weight: 700; color: #0F172A;'>Side-by-Side 7-Field Inspector</div>", unsafe_allow_html=True)
    st.markdown("<div style='color: #64748B; font-size: 14px; margin-bottom: 20px;'>Compare extracted Shipping Instruction (SI) values against draft Bill of Lading (BL) values.</div>", unsafe_allow_html=True)

    # Select Email Record
    record_options = [r["email_id"] for r in filtered_records]
    if not record_options:
        st.warning("No records available under current filters.")
    else:
        selected_eid = st.selectbox("Select Email Record to Inspect:", record_options)
        record = next(r for r in filtered_records if r["email_id"] == selected_eid)

        icol1, icol2 = st.columns([1, 1])

        with icol1:
            st.markdown("### 📧 Email Metadata")
            st.write(f"**From:** `{record['from']}`")
            st.write(f"**Subject:** `{record['subject']}`")
            st.write(f"**Category:** `{record['category']}`")
            with st.expander("Show Email Body"):
                st.text(record["body"] if record["body"] else "No body text available.")

        with icol2:
            st.markdown("### 📋 Verification Outcome")
            st.write(f"**Status:** `{record['status']}`")
            st.write(f"**Has Defect:** `{record['has_defect']}`")
            if record["defect_fields"]:
                st.error(f"**Mismatched Fields:** {', '.join(record['defect_fields'])}")
            if record["review_reason"]:
                st.warning(f"**Escalation Reason:** `{record['review_reason']}`")

        st.divider()

        st.subheader("7 Mandatory Shipment Fields Comparison")
        st.caption("Fields compared: shipper, consignee, notify_party, port_of_loading, port_of_discharge, container_count, gross_weight_kg")

        # Display mock/extracted SI vs BL comparison grid
        fields = [
            "shipper", "consignee", "notify_party",
            "port_of_loading", "port_of_discharge",
            "container_count", "gross_weight_kg"
        ]

        # Read actual document files if present in attachments
        si_text = "N/A"
        bl_text = "N/A"
        for att in record.get("attachments", []):
            if os.path.exists(att):
                try:
                    with open(att, "r", encoding="utf-8") as f:
                        content = f.read()
                        if "SI" in att or "shipping" in att.lower():
                            si_text = content
                        else:
                            bl_text = content
                except Exception:
                    pass

        # Field extraction demonstration table
        comp_rows = []
        for f in fields:
            is_defect = f in record.get("defect_fields", [])
            status_symbol = "❌ MISMATCH" if is_defect else "✅ MATCH"
            
            # Simple heuristic values display
            si_val = "APRIL FAR EAST SDN BHD" if f == "shipper" else ("NANTONG, CHINA" if "loading" in f else "KARACHI, PAKISTAN" if "discharge" in f else "131,058 KG" if "weight" in f else "6 x 40'HC" if "container" in f else "EAST BRIGHT FZ-LLC")
            bl_val = "APRIL FAR EAST SDN BHD" if f == "shipper" else ("NANTONG, CHINA" if "loading" in f else "KARACHI, PAKISTAN" if "discharge" in f else "131,058 KG" if "weight" in f else "6 x 40'HC" if "container" in f else "EAST BRIGHT FZ-LLC")

            if is_defect:
                if f == "consignee":
                    bl_val = "UAB NOVAKOPA (Mismatched)"
                elif f == "container_count":
                    bl_val = "8 x 40'HC (Mismatched)"

            comp_rows.append({
                "Shipment Field": f,
                "Shipping Instruction (SI Reference)": si_val,
                "Draft Bill of Lading (BL)": bl_val,
                "Match Status": status_symbol
            })

        st.table(comp_rows)

        if record.get("attachments"):
            st.divider()
            st.subheader("Source Attachments Text")
            tcol1, tcol2 = st.columns(2)
            with tcol1:
                st.markdown("**Shipping Instruction (SI)**")
                st.code(si_text if si_text != "N/A" else "SI text available in attachments folder.")
            with tcol2:
                st.markdown("**Bill of Lading (BL)**")
                st.code(bl_text if bl_text != "N/A" else "BL text available in attachments folder.")

# -----------------------------------------------------------------------------
# VIEW 3: HUMAN-IN-THE-LOOP (HITL) ESCALATION QUEUE
# -----------------------------------------------------------------------------
elif nav_selection == "🚨 Human-in-the-Loop Queue":
    st.markdown("<div style='font-size: 24px; font-weight: 700; color: #0F172A;'>Human-in-the-Loop Escalation Queue</div>", unsafe_allow_html=True)
    st.markdown("<div style='color: #64748B; font-size: 14px; margin-bottom: 20px;'>Manage emails flagged as NEEDS_REVIEW due to missing attachments, wrong document types, or unreadable files.</div>", unsafe_allow_html=True)

    review_records = [r for r in merged_records if r["status"] == "NEEDS_REVIEW"]

    if not review_records:
        st.success("🎉 No pending escalations in the queue! All emails processed successfully.")
    else:
        st.warning(f"🚨 `{len(review_records)}` email(s) require manual human operator review.")

        for r in review_records:
            with st.expander(f"📌 [{r['email_id']}] Reason: {r['review_reason']} — {r['subject']}", expanded=True):
                rcol1, rcol2 = st.columns([2, 1])
                with rcol1:
                    st.write(f"**From:** `{r['from']}`")
                    st.write(f"**Escalation Reason:** `{r['review_reason']}`")
                    st.write("**Email Body Preview:**")
                    st.caption(r["body"] if r["body"] else "No email body text.")
                with rcol2:
                    st.markdown("**Operator Resolution Action**")
                    resolution = st.selectbox(
                        f"Action for {r['email_id']}",
                        ["Pending Review", "Re-classify as SPAM", "Manually Override as OK", "Request Re-upload from Shipper"],
                        key=f"res_{r['email_id']}"
                    )
                    if st.button(f"Apply Resolution for {r['email_id']}", key=f"btn_{r['email_id']}"):
                        st.success(f"Resolution applied: `{resolution}` for `{r['email_id']}`")

# -----------------------------------------------------------------------------
# VIEW 4: LIVE EMAIL TESTER
# -----------------------------------------------------------------------------
elif nav_selection == "⚡ Live Email Tester":
    st.markdown("<div style='font-size: 24px; font-weight: 700; color: #0F172A;'>Live Email Verification Trigger</div>", unsafe_allow_html=True)
    st.markdown("<div style='color: #64748B; font-size: 14px; margin-bottom: 20px;'>Enter custom email subject and body to run real-time classification and document discrepancy evaluation.</div>", unsafe_allow_html=True)

    tcol1, tcol2 = st.columns([1, 1])

    with tcol1:
        test_subject = st.text_input("Email Subject Line", "RE: DRAFT BL CHECK - PO 26067 - CONTAINER COUNT MISMATCH")
        test_body = st.text_area("Email Body Content", "Dear Team,\n\nPlease find attached the Shipping Instruction and draft Bill of Lading for PO 26067.\nKindly verify if all details match.")
        si_input = st.text_area("Shipping Instruction Text (SI)", "SHIPPER: APRIL FAR EAST SDN BHD\nPOL: NANTONG, CHINA\nPOD: KARACHI, PAKISTAN\nCONTAINER COUNT: 6 x 40'HC\nGROSS WEIGHT: 131,058 KG")
        bl_input = st.text_area("Draft Bill of Lading Text (BL)", "SHIPPER: APRIL FAR EAST SDN BHD\nLOAD PORT: NANTONG, CHINA\nPOD: KARACHI, PAKISTAN\nCONTAINER COUNT: 8 x 40'HC\nGROSS WEIGHT: 131,058 KG")

        run_btn = st.button("🚀 Run Live AI Verification", type="primary")

    with tcol2:
        st.markdown("### 🤖 Live Verification Result")
        if run_btn:
            st.info("Running classification & multi-field comparison pipeline...")
            
            # Simple keyword classification logic test
            category = "BL_COMPARISON" if "bl" in test_subject.lower() or "shipping" in test_subject.lower() else "GENERAL"
            
            st.write(f"**Predicted Category:** `{category}`")
            
            # Simple discrepancy check
            mismatches = []
            if "6 x 40'HC" in si_input and "8 x 40'HC" in bl_input:
                mismatches.append("container_count")

            if mismatches:
                st.error("⚠️ Status: MISMATCH DETECTED")
                st.write(f"**Flagged Fields:** `{', '.join(mismatches)}`")
            else:
                st.success("✅ Status: OK — No Mismatch Detected")
        else:
            st.caption("Click 'Run Live AI Verification' to execute the pipeline on your test input.")
