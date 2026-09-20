# 🚢 Automated Shipping Document Verification Pipeline

Developed for the **Averis x Monash Hackathon 2026** (Shipping Document Verification Use Case).

## 📌 Project Overview
An automated intake and discrepancy identification engine designed for logistics operations teams. The pipeline processes incoming operational email inboxes, classifies message intents, extracts key shipment details from Shipping Instructions (SI) and draft Bills of Lading (BL), compares the **7 core shipping fields**, and escalates edge cases requiring human review.

---

## 🎯 Key Capabilities

### 1. Intent Classification (Stage 1)
Automatically categorizes emails into 5 distinct operational buckets:
* `BL_COMPARISON`: Requests to verify draft Bill of Lading against Shipping Instructions.
* `SI_REQUEST`: Requests to issue or prepare new Shipping Instructions.
* `INVOICE_QUERY`: Billing, tax invoice, or payment-related inquiries.
* `GENERAL`: Standard operational correspondence and updates.
* `SPAM`: Unsolicited promotional or irrelevant emails.

### 2. Edge-Case Escalation Engine (Stage 2 - `NEEDS_REVIEW`)
Flagged cases requiring Human-in-the-Loop review with explicit reason codes:
* `wrong_doc_type`: Attached document is a Commercial Invoice, Packing List, or non-BL file.
* `missing_attachment`: Comparison requested but draft BL attachment is missing.
* `unreadable`: Corrupted, garbled, or empty files.
* `missing_value`: SI contains unpopulated placeholder values (`???`, `_______`, `TBA`, `N/A`).

### 3. Field Extraction & Discrepancy Matching (Stage 3)
Normalizes label variations (e.g., *Port of Loading* vs. *POL*, *Consignee* vs. *To the Order of*) and compares the 7 required fields:
1. `shipper`
2. `consignee`
3. `notify_party`
4. `port_of_loading`
5. `port_of_discharge`
6. `container_count`
7. `gross_weight_kg`

Outputs exact side-by-side mismatch lists when discrepancies exist (`status: "MISMATCH"`).

---

## 🏗️ System Architecture & Execution Flow

```text
[ Email Inbox (JSON) ] ──► [ Stage 1: Classifier ] ──┬──► Non-BL Request ──► [ status: "OK" ]
                                                      │
                                                      └──► BL_COMPARISON
                                                                │
                                                    [ Stage 2: Quality & Edge Checks ]
                                                                │
                                                   ├──► Invalid/Missing ──► [ status: "NEEDS_REVIEW" ]

```
---

## ⚙️ Installation &amp; Setup

### Prerequisites

* Python 3.10+ installed.

### Setup Steps

1. Clone this repository:

```
git clone https://github.com/nishtwowhoo/sdoc-shipping-document-verifier.git
cd sdoc-shipping-document-verifier

```

1. Run the classification &amp; verification pipeline:

```
python classify.py

```

1. Inspect generated predictions: The output is saved to `submission.json` following the required evaluation schema.
                                                      │
                                                    [ Stage 3: Field Extraction Engine ]
                                                                │
                                                      ├──► Field Differences ──► [ status: "MISMATCH" ]
                                                      └──► 100% Field Match ──► [ status: "OK" ]
---

## 💻 Tech Stack

* **Language**: Python 3
* **Libraries**: `json`, `re` (Regular Expressions), `os`, `loader.py`
* **Data Format**: Standard JSON schema matching `sample_submission.json`
