"""Generate the RAW THREAD demo board: an Excel file for monday.com import
and a JSON file shaped like the monday.com API response (for --demo mode)."""
import json
import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

random.seed(43)
OUT = Path(__file__).resolve().parent

AS_OF = date(2026, 11, 9)  # Monday of the demo report week
OWNERS = ["Anna", "David", "Lilit", "Mark"]
SUPPLIERS = ["Ararat Textiles", "Baltic Cotton Mill", "Istanbul Print Co", "Tbilisi Denim Lab"]
PRODUCTS = [
    ("Heavyweight hoodie", 38), ("Oversized tee", 11), ("Cargo pants", 29),
    ("Denim jacket", 54), ("Knit beanie", 7), ("Crewneck sweatshirt", 26),
    ("Tote bag", 6), ("Track jacket", 41), ("Wide-leg jeans", 33), ("Coach jacket", 47),
]
COLORS = ["Black", "Bone", "Charcoal", "Olive", "Washed blue"]

rows = []
po = 1040
for i in range(32):
    po += random.randint(1, 3)
    product, unit_cost = random.choice(PRODUCTS)
    qty = random.choice([60, 80, 100, 120, 150, 200, 250, 300])
    due = AS_OF + timedelta(days=random.randint(-14, 24))
    if due < AS_OF - timedelta(days=3):
        status = random.choices(["Done", "Stuck", "Working on it"], [6, 2, 2])[0]
    elif due < AS_OF + timedelta(days=7):
        status = random.choices(["Done", "Working on it", "Stuck", "Not started"], [2, 5, 1, 2])[0]
    else:
        status = random.choices(["Working on it", "Not started"], [4, 6])[0]
    rows.append({
        "Order": f"PO-{po} {product} — {random.choice(COLORS)}",
        "Status": status,
        "Owner": random.choice(OWNERS),
        "Supplier": random.choice(SUPPLIERS),
        "Due date": due.isoformat(),
        "Quantity": qty,
        "Order value": round(qty * unit_cost * random.uniform(0.95, 1.08)),
        "Priority": random.choices(["High", "Medium", "Low"], [3, 5, 2])[0],
    })

# Excel for monday.com import (first row = headers, first column = item name)
wb = Workbook()
ws = wb.active
ws.title = "Orders"
headers = list(rows[0].keys())
ws.append(headers)
for r in rows:
    ws.append([r[h] for h in headers])
for c in ws[1]:
    c.font = Font(bold=True, color="E8E0D0")
    c.fill = PatternFill("solid", fgColor="1A1A1A")
    c.alignment = Alignment(vertical="center")
widths = {"A": 40, "B": 14, "C": 10, "D": 20, "E": 12, "F": 10, "G": 12, "H": 10}
for col, w in widths.items():
    ws.column_dimensions[col].width = w
ws.freeze_panes = "A2"
wb.save(OUT / "raw_thread_orders_board.xlsx")

# JSON shaped like the monday.com API answer the script receives
col_ids = {"Status": "status", "Owner": "text_owner", "Supplier": "text_supplier",
           "Due date": "date4", "Quantity": "numbers", "Order value": "numbers_value",
           "Priority": "priority"}
board = {
    "id": "1234567890",
    "name": "RAW THREAD — Production orders",
    "columns": [{"id": "name", "title": "Name", "type": "name"}] + [
        {"id": cid, "title": title, "type": "text"} for title, cid in col_ids.items()
    ],
    "items": [],
}
for idx, r in enumerate(rows):
    board["items"].append({
        "id": str(9000000000 + idx),
        "name": r["Order"],
        "column_values": [{"id": cid, "text": str(r[title])} for title, cid in col_ids.items()],
    })
sample = {"as_of": AS_OF.isoformat(), "board": board}
(OUT / "sample_board.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
print(len(rows), "rows written")
