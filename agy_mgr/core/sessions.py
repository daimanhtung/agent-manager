import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from agy_mgr.config import APP_CONV_DB, CLI_CONV_DB, AGY_BIN


def parse_sqlite_timestamp(ts_val) -> Optional[datetime]:
    """Parse sqlite datetime string into datetime object."""
    if not ts_val:
        return None
    try:
        if isinstance(ts_val, str):
            clean = ts_val.replace("Z", "+00:00")
            return datetime.fromisoformat(clean)
    except Exception:
        pass
    return None


def format_relative_time(dt: Optional[datetime]) -> str:
    """Format datetime into relative time string like '5m ago', '2h ago', 'yesterday'."""
    if not dt:
        return "N/A"
    try:
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now - dt
        secs = int(diff.total_seconds())
        if secs < 0:
            return "vừa xong"
        if secs < 60:
            return f"{secs}s trước"
        if secs < 3600:
            return f"{secs // 60}m trước"
        if secs < 86400:
            return f"{secs // 3600}h trước"
        days = secs // 86400
        if days == 1:
            return "hôm qua"
        if days < 7:
            return f"{days} ngày trước"
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return str(dt)


def read_db_sessions(db_path: Path, source_label: str) -> List[Dict[str, Any]]:
    """Read conversation summaries from a SQLite database file."""
    if not db_path.exists():
        return []

    sessions = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        query = """
        SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status
        FROM conversation_summaries
        ORDER BY last_modified_time DESC
        LIMIT 100
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        for r in rows:
            cid, title, preview, steps, mtime, ws_raw, status = r
            dt = parse_sqlite_timestamp(mtime)

            # parse workspace name
            ws_name = ""
            if ws_raw:
                try:
                    uris = json.loads(ws_raw)
                    if isinstance(uris, list) and uris:
                        ws_name = Path(uris[0].replace("file://", "")).name
                except Exception:
                    pass

            clean_title = (title or preview or "Không có tiêu đề").replace("\n", " ").strip()
            clean_preview = (preview or "").replace("\n", " ").strip()

            sessions.append({
                "id": cid,
                "title": clean_title,
                "preview": clean_preview,
                "steps": steps or 0,
                "modified_dt": dt,
                "modified_str": format_relative_time(dt),
                "workspace": ws_name,
                "status": status or "IDLE",
                "source": source_label
            })
        conn.close()
    except Exception:
        pass
    return sessions


def merge_syncthing_conflict_dbs():
    """
    Automatically detect and merge any Syncthing conflict databases
    (e.g., conversation_summaries.sync-conflict-*.db) into the main database.
    """
    for main_db in [APP_CONV_DB, CLI_CONV_DB]:
        if not main_db.exists():
            continue
        parent = main_db.parent
        conflicts = list(parent.glob(f"{main_db.stem}.sync-conflict-*.db"))
        for c_db in conflicts:
            try:
                conn = sqlite3.connect(main_db)
                conn.execute(f"ATTACH '{c_db}' AS other;")
                conn.execute("INSERT OR IGNORE INTO main.conversation_summaries SELECT * FROM other.conversation_summaries;")
                conn.commit()
                conn.execute("DETACH other;")
                conn.close()
                c_db.unlink(missing_ok=True)
            except Exception:
                pass


def list_all_sessions(limit: int = 25, filter_workspace: Optional[str] = None, search: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get combined, deduplicated sessions from both Antigravity App and Antigravity CLI.
    Sorted by most recent activity.
    """
    # Merge any incoming Syncthing conflict databases automatically
    merge_syncthing_conflict_dbs()

    app_sessions = read_db_sessions(APP_CONV_DB, "App")
    cli_sessions = read_db_sessions(CLI_CONV_DB, "CLI")

    combined = {}
    for s in app_sessions + cli_sessions:
        cid = s["id"]
        if cid not in combined:
            combined[cid] = s
        else:
            # Keep whichever is more recently modified
            dt_existing = combined[cid]["modified_dt"]
            dt_new = s["modified_dt"]
            if dt_new and dt_existing and dt_new > dt_existing:
                combined[cid] = s

    sessions = list(combined.values())
    sessions.sort(key=lambda x: x["modified_dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    if filter_workspace:
        sessions = [s for s in sessions if filter_workspace.lower() in s["workspace"].lower()]

    if search:
        kw = search.lower()
        sessions = [
            s for s in sessions
            if kw in s["title"].lower() or kw in s["preview"].lower() or kw in s["id"].lower()
        ]

    return sessions[:limit]


def resume_session(conversation_id: Optional[str] = None):
    """
    Resume session directly by handing over the terminal to `agy`.
    If conversation_id is provided, calls `agy --conversation <id>`.
    Otherwise calls `agy -c` (most recent).
    """
    if conversation_id:
        cmd = [AGY_BIN, "--conversation", conversation_id]
    else:
        cmd = [AGY_BIN, "-c"]

    # Replace current python process with agy for seamless TTY interaction
    try:
        os.execvp(AGY_BIN, cmd)
    except Exception:
        # Fallback to subprocess if execvp fails
        subprocess.call(cmd)
