"""
📊 Insider Hunter — Phase 3: Google Sheets Sync (sheets_sync.py)
Reads the SQLite DB and pushes Pools and Buyers to a live Google Sheet dashboard.
Respects Google API quotas (batch updates, hard row caps, polite delays).
"""

import os
import sys
import json
import sqlite3
import tempfile
import time
import gspread
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "insider_hunter.db")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

MAX_ROWS = 1000  # Hard cap to respect API quotas and keep sheet fast


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def get_db_data(conn: sqlite3.Connection, table: str, limit: int):
    """Fetches column names and rows safely from SQLite."""
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cur.fetchall()]
        
        # Order by rowid DESC to get newest first, then we reverse it later 
        # so the sheet reads chronologically top-to-bottom.
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        return columns, rows
    except sqlite3.Error as e:
        log(f"⚠️ SQLite error reading {table}: {e}")
        return [], []


def sync_sheet(gc, sheet_id: str, tab_name: str, columns: list, rows: list) -> bool:
    """Clears and batch-updates a specific worksheet tab."""
    try:
        sh = gc.open_by_key(sheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        log(f"❌ Spreadsheet {sheet_id} not found or not shared with service account.")
        return False
    except Exception as e:
        log(f"❌ Failed to open spreadsheet: {e}")
        return False
        
    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        log(f"📝 Creating new tab: {tab_name}")
        # Create with enough rows to hold our max cap + headers
        ws = sh.add_worksheet(title=tab_name, rows=MAX_ROWS + 10, cols=max(len(columns), 10))
        
    # Prepare data: convert all cells to strings to prevent gspread type errors
    str_rows = []
    for row in rows:
        str_row = [str(cell) if cell is not None else "" for cell in row]
        str_rows.append(str_row)
        
    # Reverse so oldest is at the top, newest at the bottom
    data_to_push = [columns] + str_rows[::-1]
    
    try:
        ws.clear()
        if data_to_push and len(data_to_push) > 1:
            # Batch update (1 API call instead of row-by-row)
            ws.update(data_to_push)
        return True
    except Exception as e:
        log(f"⚠️ Failed to update {tab_name}: {e}")
        return False


def main() -> None:
    log("📊 Insider Hunter — Sheets Sync starting...")
    
    if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_ID:
        log("⚠️ GOOGLE_CREDENTIALS_JSON or GOOGLE_SHEET_ID missing. Skipping sync.")
        return
        
    tmp_path = None
    try:
        # Validate JSON before writing
        json.loads(GOOGLE_CREDENTIALS_JSON)
        
        # Write to temp file for gspread compatibility across all versions
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.json', delete=False) as tmp:
            tmp.write(GOOGLE_CREDENTIALS_JSON)
            tmp_path = tmp.name
            
        gc = gspread.service_account(filename=tmp_path)
    except json.JSONDecodeError as e:
        log(f"❌ GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}")
        return
    except Exception as e:
        log(f"❌ Failed to authenticate with Google: {e}")
        return
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
            
    if not os.path.exists(DB_PATH):
        log("⚠️ DB file not found. Nothing to sync.")
        return

    conn = sqlite3.connect(DB_PATH)
    
    # Sync Pools
    log("🔄 Syncing Pools...")
    cols, rows = get_db_data(conn, "pools", MAX_ROWS)
    if sync_sheet(gc, GOOGLE_SHEET_ID, "Pools", cols, rows):
        log(f"✅ Pushed {len(rows)} pools to Sheets.")
        
    time.sleep(2)  # Polite delay to respect Google's 60 req/min free quota
    
    # Sync Buyers
    log("🔄 Syncing Buyers...")
    cols, rows = get_db_data(conn, "buyers", MAX_ROWS)
    if sync_sheet(gc, GOOGLE_SHEET_ID, "Buyers", cols, rows):
        log(f"✅ Pushed {len(rows)} buyers to Sheets.")
        
    conn.close()
    log("🏁 Sheets Sync complete.")


if __name__ == "__main__":
    main()
