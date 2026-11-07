"""Weekly PDF report from a monday.com board.

Demo (no account needed):
    python3 monday_report.py --demo

Live board:
    export MONDAY_API_TOKEN="your token"
    python3 monday_report.py --board 1234567890

The script reads the board through the monday.com GraphQL API, counts open,
overdue and stuck items, draws two charts and writes a one-page PDF to ./output.
Column titles are set in COLUMNS below, so the same script fits any board.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import Table, TableStyle  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

# --- Board settings: column titles exactly as they appear on the board -------
COLUMNS = {
    "status": "Status",
    "owner": "Owner",
    "supplier": "Supplier",
    "due": "Due date",
    "quantity": "Quantity",
    "value": "Order value",
    "priority": "Priority",
}
DONE_LABELS = {"Done"}
STUCK_LABELS = {"Stuck"}
CURRENCY = "$"

API_URL = "https://api.monday.com/v2"
PAGE_SIZE = 100

# --- Brand palette (RAW THREAD) ----------------------------------------------
CHARCOAL = "#1A1A1A"
BONE = "#E8E0D0"
INK_SOFT = "#5C5853"
RULE = "#D9D2C5"
ALERT = "#B5532E"
STATUS_COLORS = {
    "Done": "#6B7A4B",
    "Working on it": "#C9A66B",
    "Stuck": ALERT,
    "Not started": "#9A958C",
}

HERE = Path(__file__).resolve().parent
DEMO_FILE = HERE / "sample_data" / "sample_board.json"


# --- 1. Get the board ----------------------------------------------------------
BOARD_QUERY = """
query ($ids: [ID!], $limit: Int!) {
  boards(ids: $ids) {
    id
    name
    columns { id title type }
    items_page(limit: $limit) {
      cursor
      items { id name column_values { id text } }
    }
  }
}
"""

NEXT_QUERY = """
query ($cursor: String!, $limit: Int!) {
  next_items_page(cursor: $cursor, limit: $limit) {
    cursor
    items { id name column_values { id text } }
  }
}
"""


def call_api(query: str, variables: dict, token: str, api_version: str | None) -> dict:
    headers = {"Authorization": token, "Content-Type": "application/json"}
    if api_version:
        headers["API-Version"] = api_version
    resp = requests.post(API_URL, json={"query": query, "variables": variables},
                         headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"monday.com API error: {payload['errors']}")
    return payload["data"]


def fetch_board(board_id: str, token: str, api_version: str | None) -> dict:
    data = call_api(BOARD_QUERY, {"ids": [board_id], "limit": PAGE_SIZE}, token, api_version)
    if not data["boards"]:
        raise RuntimeError(f"Board {board_id} not found or no access with this token.")
    raw = data["boards"][0]
    items = list(raw["items_page"]["items"])
    cursor = raw["items_page"]["cursor"]
    while cursor:  # monday.com returns items in pages; the cursor is null on the last one
        page = call_api(NEXT_QUERY, {"cursor": cursor, "limit": PAGE_SIZE}, token, api_version)
        items.extend(page["next_items_page"]["items"])
        cursor = page["next_items_page"]["cursor"]
    return {"id": raw["id"], "name": raw["name"], "columns": raw["columns"], "items": items}


def load_demo() -> tuple[dict, date]:
    sample = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
    return sample["board"], date.fromisoformat(sample["as_of"])


# --- 2. Turn it into a table ---------------------------------------------------
def to_frame(board: dict) -> pd.DataFrame:
    title_by_id = {c["id"]: c["title"] for c in board["columns"]}
    records = []
    for item in board["items"]:
        row = {"Item": item["name"]}
        for cv in item["column_values"]:
            row[title_by_id.get(cv["id"], cv["id"])] = cv.get("text") or ""
        records.append(row)
    df = pd.DataFrame(records)

    missing = [t for t in COLUMNS.values() if t not in df.columns]
    if missing:
        raise RuntimeError(
            f"Columns not found on the board: {', '.join(missing)}. "
            "Rename them on the board or edit COLUMNS at the top of the script."
        )

    out = pd.DataFrame({
        "item": df["Item"],
        "status": df[COLUMNS["status"]].replace("", "Not started"),
        "owner": df[COLUMNS["owner"]].replace("", "Unassigned"),
        "supplier": df[COLUMNS["supplier"]],
        "due": pd.to_datetime(df[COLUMNS["due"]].str[:10], errors="coerce").dt.date,
        "quantity": pd.to_numeric(df[COLUMNS["quantity"]], errors="coerce").fillna(0),
        "value": pd.to_numeric(df[COLUMNS["value"]], errors="coerce").fillna(0),
        "priority": df[COLUMNS["priority"]],
    })
    return out


# --- 3. Count what matters -----------------------------------------------------
@dataclass
class Summary:
    total: int
    done: int
    open_: int
    due_week: int
    overdue: int
    stuck: int
    open_value: float
    attention: pd.DataFrame
    upcoming: pd.DataFrame


def summarize(df: pd.DataFrame, today: date) -> Summary:
    is_done = df["status"].isin(DONE_LABELS)
    open_df = df[~is_done].copy()
    week_end = today + timedelta(days=6)

    has_due = open_df["due"].notna()
    overdue_mask = has_due & (open_df["due"] < today)
    stuck_mask = open_df["status"].isin(STUCK_LABELS)
    week_mask = has_due & (open_df["due"] >= today) & (open_df["due"] <= week_end)

    attention = open_df[overdue_mask | stuck_mask].copy()
    attention["days_late"] = attention["due"].apply(
        lambda d: (today - d).days if pd.notna(d) and d < today else 0)
    attention = attention.sort_values(["days_late", "value"], ascending=[False, False])

    upcoming = open_df[week_mask].sort_values(["due", "value"], ascending=[True, False])

    return Summary(
        total=len(df), done=int(is_done.sum()), open_=len(open_df),
        due_week=int(week_mask.sum()), overdue=int(overdue_mask.sum()),
        stuck=int(stuck_mask.sum()), open_value=float(open_df["value"].sum()),
        attention=attention, upcoming=upcoming,
    )


# --- 4. Charts -----------------------------------------------------------------
def style_axes(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(RULE)
    ax.tick_params(colors=INK_SOFT, labelsize=8, length=0)
    ax.xaxis.grid(True, color=RULE, linewidth=0.6)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_axisbelow(True)


def chart_status(df: pd.DataFrame, path: Path):
    order = ["Done", "Working on it", "Stuck", "Not started"]
    counts = df["status"].value_counts()
    labels = [s for s in order if s in counts] + [s for s in counts.index if s not in order]
    values = [int(counts[s]) for s in labels]
    fig, ax = plt.subplots(figsize=(3.6, 2.2), dpi=200)
    bars = ax.barh(labels[::-1], values[::-1],
                   color=[STATUS_COLORS.get(s, INK_SOFT) for s in labels[::-1]], height=0.6)
    for b, v in zip(bars, values[::-1]):
        ax.text(b.get_width() + 0.3, b.get_y() + b.get_height() / 2, str(v),
                va="center", fontsize=8, color=CHARCOAL)
    style_axes(ax)
    ax.set_xlim(0, max(values) * 1.18)
    fig.tight_layout()
    fig.savefig(path, transparent=True)
    plt.close(fig)


def chart_owner(df: pd.DataFrame, today: date, path: Path):
    open_df = df[~df["status"].isin(DONE_LABELS)].copy()
    open_df["late"] = open_df["due"].apply(lambda d: pd.notna(d) and d < today)
    grouped = open_df.groupby("owner")["late"].agg(total="count", late="sum").sort_values("total")
    on_time = grouped["total"] - grouped["late"]
    fig, ax = plt.subplots(figsize=(3.6, 2.2), dpi=200)
    ax.barh(grouped.index, on_time, color=INK_SOFT, height=0.6, label="On time")
    ax.barh(grouped.index, grouped["late"], left=on_time, color=ALERT, height=0.6, label="Overdue")
    for y, total in enumerate(grouped["total"]):
        ax.text(total + 0.2, y, str(int(total)), va="center", fontsize=8, color=CHARCOAL)
    style_axes(ax)
    ax.set_xlim(0, grouped["total"].max() * 1.2)
    ax.legend(frameon=False, fontsize=7, loc="upper center", bbox_to_anchor=(0.45, -0.1),
              ncol=2, labelcolor=INK_SOFT)
    fig.tight_layout()
    fig.savefig(path, transparent=True)
    plt.close(fig)


# --- 5. PDF --------------------------------------------------------------------
def money(v: float) -> str:
    return f"{CURRENCY}{v:,.0f}"


def short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def draw_table(c, rows, col_widths, x, y_top, header_bg=CHARCOAL):
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 7.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(BONE)),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor(CHARCOAL)),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor(RULE)),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    _, h = t.wrap(0, 0)
    t.drawOn(c, x, y_top - h)
    return h


def build_pdf(board: dict, df: pd.DataFrame, s: Summary, today: date, out_path: Path):
    W, H = A4
    M = 16 * mm
    c = canvas.Canvas(str(out_path), pagesize=A4)
    c.setTitle(f"Weekly report — {board['name']}")

    # Header band
    c.setFillColor(colors.HexColor(CHARCOAL))
    c.rect(0, H - 34 * mm, W, 34 * mm, stroke=0, fill=1)
    c.setFillColor(colors.HexColor(BONE))
    c.setFont("Helvetica", 8)
    c.drawString(M, H - 12 * mm, "WEEKLY BOARD REPORT  ·  MONDAY.COM")
    c.setFont("Helvetica-Bold", 17)
    c.drawString(M, H - 21 * mm, board["name"])
    c.setFont("Helvetica", 9)
    week_end = today + timedelta(days=6)
    c.drawString(M, H - 28 * mm,
                 f"Week of {today:%d %b %Y} – {week_end:%d %b %Y}   ·   {s.total} items on the board")

    # KPI tiles
    tiles = [
        ("Open orders", str(s.open_), f"{s.done} done"),
        ("Due this week", str(s.due_week), "next 7 days"),
        ("Overdue", str(s.overdue), "past due, not done"),
        ("Stuck", str(s.stuck), "marked Stuck"),
        ("Open order value", money(s.open_value), "not yet delivered"),
    ]
    gap = 3 * mm
    tile_w = (W - 2 * M - gap * (len(tiles) - 1)) / len(tiles)
    tile_h = 22 * mm
    y_tiles = H - 34 * mm - 8 * mm - tile_h
    for i, (label, value, note) in enumerate(tiles):
        x = M + i * (tile_w + gap)
        alert = label in ("Overdue", "Stuck") and value != "0"
        c.setFillColor(colors.HexColor(BONE))
        c.roundRect(x, y_tiles, tile_w, tile_h, 2 * mm, stroke=0, fill=1)
        c.setFillColor(colors.HexColor(INK_SOFT))
        c.setFont("Helvetica", 7)
        c.drawString(x + 3 * mm, y_tiles + tile_h - 6 * mm, label.upper())
        c.setFillColor(colors.HexColor(ALERT if alert else CHARCOAL))
        c.setFont("Helvetica-Bold", 15 if len(value) < 8 else 12)
        c.drawString(x + 3 * mm, y_tiles + 8 * mm, value)
        c.setFillColor(colors.HexColor(INK_SOFT))
        c.setFont("Helvetica", 6.5)
        c.drawString(x + 3 * mm, y_tiles + 3.5 * mm, note)

    # Charts
    y_chart_title = y_tiles - 9 * mm
    chart_w = (W - 2 * M - 6 * mm) / 2
    chart_h = chart_w * 2.2 / 3.6
    with tempfile.TemporaryDirectory() as tmp:
        p1, p2 = Path(tmp) / "status.png", Path(tmp) / "owner.png"
        chart_status(df, p1)
        chart_owner(df, today, p2)
        for i, (title, p) in enumerate([("Items by status", p1), ("Open items by owner", p2)]):
            x = M + i * (chart_w + 6 * mm)
            c.setFillColor(colors.HexColor(CHARCOAL))
            c.setFont("Helvetica-Bold", 9.5)
            c.drawString(x, y_chart_title, title)
            c.drawImage(str(p), x - 2 * mm, y_chart_title - 3 * mm - chart_h,
                        width=chart_w, height=chart_h, mask="auto")
    y = y_chart_title - 3 * mm - chart_h - 8 * mm

    # Needs attention
    c.setFillColor(colors.HexColor(CHARCOAL))
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(M, y, f"Needs attention ({len(s.attention)})")
    c.setFillColor(colors.HexColor(INK_SOFT))
    c.setFont("Helvetica", 7.5)
    c.drawRightString(W - M, y, "overdue or stuck, most late first")
    y -= 3 * mm
    rows = [["Order", "Owner", "Supplier", "Due", "Status", "Days late"]]
    for _, r in s.attention.head(10).iterrows():
        rows.append([short(r["item"], 42), r["owner"], short(r["supplier"], 20),
                     r["due"].strftime("%d %b") if pd.notna(r["due"]) else "—",
                     r["status"], str(r["days_late"]) if r["days_late"] else "—"])
    if len(rows) == 1:
        rows.append(["Nothing overdue or stuck this week", "", "", "", "", ""])
    widths = [70 * mm, 20 * mm, 32 * mm, 16 * mm, 23 * mm, 17 * mm]
    y -= draw_table(c, rows, widths, M, y) + 8 * mm

    # Due this week
    c.setFillColor(colors.HexColor(CHARCOAL))
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(M, y, f"Due in the next 7 days ({len(s.upcoming)})")
    y -= 3 * mm
    rows = [["Order", "Owner", "Supplier", "Due", "Status", "Value"]]
    for _, r in s.upcoming.head(10).iterrows():
        rows.append([short(r["item"], 42), r["owner"], short(r["supplier"], 20),
                     r["due"].strftime("%d %b"), r["status"], money(r["value"])])
    if len(rows) == 1:
        rows.append(["Nothing due in the next 7 days", "", "", "", "", ""])
    draw_table(c, rows, widths, M, y, header_bg=INK_SOFT)

    # Footer
    c.setStrokeColor(colors.HexColor(RULE))
    c.line(M, 14 * mm, W - M, 14 * mm)
    c.setFillColor(colors.HexColor(INK_SOFT))
    c.setFont("Helvetica", 7)
    c.drawString(M, 10 * mm, f"Source: monday.com board {board['id']}  ·  data as of {today:%d %b %Y}")
    c.drawRightString(W - M, 10 * mm, "Generated automatically with Python")
    c.showPage()
    c.save()


# --- Run -----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly PDF report from a monday.com board")
    ap.add_argument("--board", help="monday.com board ID (the number in the board URL)")
    ap.add_argument("--demo", action="store_true", help="use the bundled RAW THREAD sample board")
    ap.add_argument("--today", help="report date YYYY-MM-DD (default: today)")
    ap.add_argument("--api-version", default=os.getenv("MONDAY_API_VERSION"),
                    help="optional monday.com API version, e.g. 2026-10")
    ap.add_argument("--out", default=str(HERE / "output"), help="folder for the PDF")
    args = ap.parse_args()

    if args.demo:
        board, demo_today = load_demo()
        today = date.fromisoformat(args.today) if args.today else demo_today
    elif args.board:
        token = os.getenv("MONDAY_API_TOKEN")
        if not token:
            print("Set MONDAY_API_TOKEN first: export MONDAY_API_TOKEN=\"your token\"")
            return 1
        board = fetch_board(args.board, token, args.api_version)
        today = date.fromisoformat(args.today) if args.today else date.today()
    else:
        ap.print_help()
        return 1

    df = to_frame(board)
    s = summarize(df, today)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"weekly_report_{today:%Y-%m-%d}.pdf"
    build_pdf(board, df, s, today, out_path)
    print(f"Saved {out_path}")
    print(f"Open: {s.open_}  Due this week: {s.due_week}  Overdue: {s.overdue}  Stuck: {s.stuck}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
