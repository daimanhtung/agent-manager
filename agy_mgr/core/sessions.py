import json
import os
import re
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


def read_db_sessions(db_path: Path, source_label: str, limit: int = 500) -> List[Dict[str, Any]]:
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
        LIMIT ?
        """
        cursor.execute(query, (limit,))
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


def list_all_sessions(limit: Optional[int] = 50, filter_workspace: Optional[str] = None, search: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get combined, deduplicated sessions from both Antigravity App and Antigravity CLI.
    Sorted by most recent activity.
    Read-only and safe: never triggers background imports or overwrites.
    """
    fetch_limit = 1000 if (not limit or limit <= 0) else max(limit * 2, 200)
    app_sessions = read_db_sessions(APP_CONV_DB, "App", limit=fetch_limit)
    cli_sessions = read_db_sessions(CLI_CONV_DB, "CLI", limit=fetch_limit)

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

    if limit and limit > 0:
        return sessions[:limit]
    return sessions


def find_transcript_file(conversation_id: str) -> Optional[Path]:
    """Find transcript.jsonl path for a given conversation_id (supports full UUID or prefix)."""
    bases = [
        Path.home() / ".gemini" / "antigravity" / "brain",
        Path.home() / ".gemini" / "antigravity-cli" / "brain",
    ]
    # Exact match first
    for base in bases:
        exact = base / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        if exact.exists():
            return exact

    # Prefix match (e.g. 8-char short ID)
    for base in bases:
        if base.exists():
            for d in base.iterdir():
                if d.is_dir() and d.name.startswith(conversation_id):
                    cand = d / ".system_generated" / "logs" / "transcript.jsonl"
                    if cand.exists():
                        return cand
    return None


def read_session_turns(conversation_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Parse readable conversation turns from transcript.jsonl."""
    p = find_transcript_file(conversation_id)
    if not p:
        return []

    turns = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    src = d.get("source")
                    stype = d.get("type")
                    content = d.get("content") or ""
                    ts = d.get("created_at")

                    if stype == "USER_INPUT" and src == "USER_EXPLICIT":
                        clean = re.sub(r'<USER_REQUEST>\n?', '', content)
                        clean = re.sub(r'</USER_REQUEST>.*', '', clean, flags=re.DOTALL).strip()
                        if clean:
                            turns.append({
                                "role": "USER",
                                "content": clean,
                                "timestamp": ts
                            })
                    elif stype == "PLANNER_RESPONSE" and src == "MODEL" and content:
                        turns.append({
                            "role": "AI",
                            "content": content.strip(),
                            "timestamp": ts
                        })
                except Exception:
                    pass
    except Exception:
        pass

    if limit and len(turns) > limit:
        return turns[-limit:]
    return turns


def print_session_log(conversation_id: str, limit: Optional[int] = None):
    """Print readable formatted chat log of a session."""
    turns = read_session_turns(conversation_id, limit=limit)
    if not turns:
        print(f"[!] Không tìm thấy dữ liệu hội thoại trong brain của session: {conversation_id}")
        return

    from agy_mgr.ui.formatters import BOLD, GREEN, CYAN, RESET, DIM

    title_suffix = f"({limit} lượt gần nhất)" if limit else "(Toàn bộ)"
    print(f"\n{BOLD}{CYAN}=== LỊCH SỬ HỘI THOẠI SESSION: {conversation_id[:8]} {title_suffix} ==={RESET}\n")

    for t in turns:
        role = t["role"]
        ts = t.get("timestamp") or ""
        ts_str = ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                ts_str = dt.strftime("%d/%m %H:%M")
            except Exception:
                ts_str = ts

        if role == "USER":
            print(f"{BOLD}{GREEN}👤 Người dùng [{ts_str}]:{RESET}")
            for l in t["content"].splitlines():
                print(f"   {l}")
        else:
            print(f"{BOLD}{CYAN}🤖 Antigravity [{ts_str}]:{RESET}")
            for l in t["content"].splitlines():
                print(f"   {l}")
        print()

    print(f"{DIM}─" * 80 + f"{RESET}\n")


def resume_session(conversation_id: Optional[str] = None, show_log: bool = True, log_limit: int = 3):
    """
    Resume session directly by handing over the terminal to `agy`.
    If show_log is True, displays the last log_limit conversation turns before entering.
    """
    full_id = conversation_id
    if conversation_id:
        p = find_transcript_file(conversation_id)
        if p:
            full_id = p.parent.parent.parent.name
        if show_log:
            print_session_log(full_id, limit=log_limit)

    if full_id:
        cmd = [AGY_BIN, "--conversation", full_id]
    else:
        cmd = [AGY_BIN, "-c"]

    # Replace current python process with agy for seamless TTY interaction
    try:
        os.execvp(AGY_BIN, cmd)
    except Exception:
        # Fallback to subprocess if execvp fails
        subprocess.call(cmd)
