"""Recurring Expense Finder.

Backend that ingests a bank statement (CSV or Excel) in an arbitrary layout,
heuristically identifies the date/description/amount fields (no LLM needed),
and detects expenses that recur on a monthly basis.
"""
import io
import re
import statistics
import warnings
from collections import defaultdict

import pandas as pd
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

MAX_HEADER_SCAN_ROWS = 20
MIN_OCCURRENCES = 3
MAX_AMOUNT_VARIATION = 0.20  # coefficient of variation allowed between occurrences
MIN_MONTH_GAP_CONSISTENCY = 0.6  # share of month-to-month gaps that must equal 1

HEADER_KEYWORDS = {
    "date": ["date", "posted", "trans date", "transaction date", "value date"],
    "description": [
        "description", "memo", "payee", "narrative", "details",
        "merchant", "transaction", "particulars",
    ],
    "amount": ["amount", "amt", "value"],
    "debit": ["debit", "withdrawal", "money out", "paid out"],
    "credit": ["credit", "deposit", "money in", "paid in"],
    "balance": ["balance"],
    "type": ["type", "transaction type", "dr/cr", "dr cr"],
}


def _cell_text(value):
    return "" if pd.isna(value) else str(value).strip()


def detect_header_row(raw):
    """Scan the first rows for the one that looks most like a header."""
    best_row, best_score = 0, -1
    for idx in range(min(MAX_HEADER_SCAN_ROWS, len(raw))):
        cells = [_cell_text(c).lower() for c in raw.iloc[idx].tolist() if _cell_text(c)]
        score = 0
        for cell in cells:
            for keywords in HEADER_KEYWORDS.values():
                if any(kw in cell for kw in keywords):
                    score += 1
                    break
        if score > best_score:
            best_row, best_score = idx, score
    return best_row


def build_dataframe(raw):
    header_row = detect_header_row(raw)
    header = [
        _cell_text(c) or f"column_{i}" for i, c in enumerate(raw.iloc[header_row].tolist())
    ]
    df = raw.iloc[header_row + 1:].copy()
    df.columns = header
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
    return df.reset_index(drop=True)


def _date_parse_ratio(sample):
    if sample.empty:
        return 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(sample, errors="coerce")
    return parsed.notna().mean()


def _numeric_parse_ratio(sample):
    if sample.empty:
        return 0.0
    cleaned = sample.str.replace(r"[,$()]", "", regex=True).str.strip()
    cleaned = cleaned.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    parsed = pd.to_numeric(cleaned, errors="coerce")
    return parsed.notna().mean()


def _text_score(sample):
    if sample.empty:
        return 0.0
    avg_len = sample.str.len().mean()
    non_numeric_ratio = 1 - _numeric_parse_ratio(sample)
    return non_numeric_ratio * min(avg_len / 15, 1.0)


def classify_columns(df):
    """Score every column against each field, then greedily assign best matches."""
    scores = {}
    for col in df.columns:
        header = str(col).strip().lower()
        sample = df[col].dropna().astype(str).head(50)

        def kw_hit(field):
            return any(kw in header for kw in HEADER_KEYWORDS[field])

        scores[col] = {
            "date": (2 if kw_hit("date") else 0) + _date_parse_ratio(sample) * 3,
            "description": (2 if kw_hit("description") else 0) + _text_score(sample) * 3,
            "amount": (2 if kw_hit("amount") else 0) + _numeric_parse_ratio(sample) * 3,
            "debit": (3 if kw_hit("debit") else 0) + _numeric_parse_ratio(sample),
            "credit": (3 if kw_hit("credit") else 0) + _numeric_parse_ratio(sample),
            "type": 3 if kw_hit("type") else 0,
        }
        if kw_hit("balance"):
            scores[col]["amount"] *= 0.3  # de-prioritize running balance columns

    candidates = []
    for col, field_scores in scores.items():
        for field, score in field_scores.items():
            if score > 0:
                candidates.append((score, col, field))
    candidates.sort(reverse=True)

    mapping = {}
    used_cols, used_fields_single = set(), set()
    for score, col, field in candidates:
        if col in used_cols:
            continue
        if field in ("date", "description", "amount") and field in used_fields_single:
            continue
        mapping[field] = col
        used_cols.add(col)
        if field in ("date", "description", "amount"):
            used_fields_single.add(field)

    return mapping


def _to_numeric_series(series):
    cleaned = series.astype(str).str.replace(r"[,$]", "", regex=True).str.strip()
    cleaned = cleaned.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def normalize_transactions(df, mapping):
    if "date" not in mapping or "description" not in mapping:
        raise ValueError("Could not identify date and description columns in this file.")

    dates = pd.to_datetime(df[mapping["date"]], errors="coerce")
    descriptions = df[mapping["description"]].astype(str)

    if "debit" in mapping and "credit" in mapping:
        debit = _to_numeric_series(df[mapping["debit"]]).fillna(0)
        credit = _to_numeric_series(df[mapping["credit"]]).fillna(0)
        amounts = credit - debit
    elif "amount" in mapping:
        amounts = _to_numeric_series(df[mapping["amount"]])
        if "type" in mapping and (amounts >= 0).mean() > 0.95:
            # Values are unsigned; use a type column to flag expenses as negative.
            type_col = df[mapping["type"]].astype(str).str.lower()
            is_expense = type_col.str.contains("debit|withdrawal|dr|out", regex=True)
            amounts = amounts.where(~is_expense, -amounts)
    else:
        raise ValueError("Could not identify an amount column in this file.")

    transactions = []
    for date, desc, amount in zip(dates, descriptions, amounts):
        if pd.isna(date) or pd.isna(amount) or not desc.strip():
            continue
        transactions.append({"date": date.to_pydatetime(), "description": desc.strip(), "amount": float(amount)})
    return transactions


def normalize_description(desc):
    s = desc.upper()
    s = re.sub(r"\d+", "", s)
    s = re.sub(r"[^A-Z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find_recurring(transactions):
    groups = defaultdict(list)
    for t in transactions:
        if t["amount"] >= 0:
            continue  # only consider money leaving the account
        key = normalize_description(t["description"])
        if key:
            groups[key].append(t)

    recurring = []
    for txs in groups.values():
        if len(txs) < MIN_OCCURRENCES:
            continue

        txs.sort(key=lambda t: t["date"])
        amounts = [abs(t["amount"]) for t in txs]
        mean_amount = statistics.mean(amounts)
        if mean_amount == 0:
            continue
        if statistics.pstdev(amounts) / mean_amount > MAX_AMOUNT_VARIATION:
            continue

        months = sorted({(t["date"].year, t["date"].month) for t in txs})
        if len(months) < MIN_OCCURRENCES:
            continue

        month_indices = [y * 12 + m for y, m in months]
        gaps = [b - a for a, b in zip(month_indices, month_indices[1:])]
        if not gaps or sum(g == 1 for g in gaps) / len(gaps) < MIN_MONTH_GAP_CONSISTENCY:
            continue

        recurring.append({
            "description": txs[-1]["description"],
            "average_amount": round(mean_amount, 2),
            "occurrences": len(txs),
            "months_seen": len(months),
            "first_date": txs[0]["date"].strftime("%Y-%m-%d"),
            "last_date": txs[-1]["date"].strftime("%Y-%m-%d"),
        })

    recurring.sort(key=lambda r: -r["average_amount"])
    return recurring


def load_raw_dataframe(file_storage):
    filename = (file_storage.filename or "").lower()
    content = file_storage.read()

    if filename.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(content), header=None, dtype=str)

    # Fall back to CSV; let pandas sniff the delimiter.
    return pd.read_csv(io.BytesIO(content), header=None, dtype=str, sep=None, engine="python")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if "file" not in request.files:
        return jsonify({"error": "No file was uploaded."}), 400

    file_storage = request.files["file"]
    if not file_storage.filename:
        return jsonify({"error": "No file was selected."}), 400

    try:
        raw = load_raw_dataframe(file_storage)
        df = build_dataframe(raw)
        mapping = classify_columns(df)
        transactions = normalize_transactions(df, mapping)
        recurring = find_recurring(transactions)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception:
        return jsonify({"error": "Could not read this file. Please check the format and try again."}), 400

    return jsonify({
        "column_mapping": mapping,
        "transaction_count": len(transactions),
        "recurring": recurring,
    })


if __name__ == "__main__":
    app.run(debug=True)
