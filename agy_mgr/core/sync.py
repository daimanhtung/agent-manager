import json
import os
import shutil
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from agy_mgr.config import (
    APP_CONV_DB,
    CLI_CONV_DB,
    GEMINI_HOME,
    SYNC_DIR,
)
from agy_mgr.core.sessions import parse_sqlite_timestamp, format_relative_time

# SQLite table creation schema if target DB or table doesn't exist
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `conversation_summaries` (
    `conversation_id` text,
    `title` text NOT NULL DEFAULT "",
    `preview` text NOT NULL DEFAULT "",
    `step_count` integer NOT NULL DEFAULT 0,
    `last_modified_time` datetime NOT NULL,
    `workspace_uris` text NOT NULL,
    `status` text NOT NULL DEFAULT "",
    `source` text NOT NULL DEFAULT "",
    `project_id` text NOT NULL DEFAULT "",
    `agent_name` text NOT NULL DEFAULT "",
    `parent_conversation_id` text NOT NULL DEFAULT "",
    `nesting_depth` integer NOT NULL DEFAULT 0,
    `battle_id` text NOT NULL DEFAULT "",
    `winning_conversation_id` text NOT NULL DEFAULT "",
    `not_fully_idle` numeric NOT NULL DEFAULT false,
    `killed` numeric NOT NULL DEFAULT false,
    `last_user_input_time` datetime NOT NULL,
    `last_user_input_step_index` integer NOT NULL DEFAULT -1,
    `app_data_dir` text NOT NULL DEFAULT "",
    `raw_summary` blob,
    `group_id` text NOT NULL DEFAULT "",
    PRIMARY KEY (`conversation_id`)
);
"""

# DB columns we serialize and deserialize
DB_COLUMNS = [
    "conversation_id", "title", "preview", "step_count", "last_modified_time",
    "workspace_uris", "status", "source", "project_id", "agent_name",
    "parent_conversation_id", "nesting_depth", "battle_id", "winning_conversation_id",
    "not_fully_idle", "killed", "last_user_input_time", "last_user_input_step_index",
    "app_data_dir", "group_id"
]


def init_db_if_needed(db_path: Path):
    """Ensure SQLite database and conversation_summaries table exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
        conn.close()
    except Exception:
        pass


def find_local_brain_dir(conversation_id: str, app_data_dir: Optional[str] = None) -> Optional[Path]:
    """Find the local brain directory for a given conversation_id."""
    candidates = []
    if app_data_dir:
        candidates.append(GEMINI_HOME / app_data_dir / "brain" / conversation_id)
    candidates.extend([
        GEMINI_HOME / "antigravity" / "brain" / conversation_id,
        GEMINI_HOME / "antigravity-cli" / "brain" / conversation_id,
    ])
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    return None


def copy_brain_tree(src: Path, dst: Path):
    """
    Safely copy brain directory from src to dst.
    Updates newer files and creates missing ones, avoiding massive temp files.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        target_root = dst / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        for f in files:
            src_file = Path(root) / f
            dst_file = target_root / f
            try:
                # Skip files larger than 50MB to keep sync fast and light
                if src_file.stat().st_size > 50 * 1024 * 1024:
                    continue
                if not dst_file.exists() or src_file.stat().st_mtime > dst_file.stat().st_mtime:
                    shutil.copy2(src_file, dst_file)
            except Exception:
                pass


def export_sessions(
    session_ids: Optional[List[str]] = None,
    limit: int = 50,
    force: bool = False
) -> Dict[str, Any]:
    """
    Export local sessions and their brain data into the repository sync folder (SYNC_DIR).
    Syncthing will then automatically sync these files to other devices.
    """
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    hostname = socket.gethostname()
    now_iso = datetime.now(timezone.utc).isoformat()

    stats = {"exported": 0, "skipped": 0, "errors": 0}

    # Gather rows from both APP and CLI DBs
    rows_by_id = {}
    for db_path in [APP_CONV_DB, CLI_CONV_DB]:
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cols_str = ", ".join([f"`{c}`" for c in DB_COLUMNS])
            query = f"SELECT {cols_str} FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT ?"
            cursor.execute(query, (limit if not session_ids else 500,))
            for r in cursor.fetchall():
                cid = r["conversation_id"]
                if session_ids and cid not in session_ids and not any(cid.startswith(s) for s in session_ids):
                    continue
                dt = parse_sqlite_timestamp(r["last_modified_time"])
                if cid not in rows_by_id:
                    rows_by_id[cid] = (dict(r), dt)
                else:
                    existing_dt = rows_by_id[cid][1]
                    if dt and existing_dt and dt > existing_dt:
                        rows_by_id[cid] = (dict(r), dt)
            conn.close()
        except Exception:
            pass

    for cid, (row_dict, local_dt) in rows_by_id.items():
        try:
            sess_sync_dir = SYNC_DIR / cid
            meta_file = sess_sync_dir / "meta.json"

            # Check if sync directory already has an equal or newer version
            if meta_file.exists() and not force:
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta_data = json.load(f)
                    remote_dt = parse_sqlite_timestamp(meta_data.get("last_modified_time"))
                    if remote_dt and local_dt and remote_dt >= local_dt:
                        stats["skipped"] += 1
                        continue
                except Exception:
                    pass

            sess_sync_dir.mkdir(parents=True, exist_ok=True)

            # Copy brain directory
            brain_src = find_local_brain_dir(cid, row_dict.get("app_data_dir"))
            if brain_src:
                brain_dst = sess_sync_dir / "brain"
                copy_brain_tree(brain_src, brain_dst)

            # Prepare meta.json
            meta_to_save = dict(row_dict)
            meta_to_save["_sync_exported_at"] = now_iso
            meta_to_save["_sync_exported_by"] = hostname

            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta_to_save, f, indent=2, ensure_ascii=False)

            stats["exported"] += 1
        except Exception:
            stats["errors"] += 1

    return stats


def adapt_workspace_uris_for_local_machine(ws_raw: str) -> str:
    """
    Adapt workspace URIs from a remote machine so that Antigravity Desktop App
    on the current machine matches the workspace and displays the session in the sidebar.
    """
    if not ws_raw:
        return ws_raw
    try:
        uris = json.loads(ws_raw)
        if not isinstance(uris, list) or not uris:
            return ws_raw

        adapted_uris = list(uris)
        home = Path.home()

        for uri in uris:
            if not uri.startswith("file://"):
                continue
            path_str = uri.replace("file://", "")
            remote_path = Path(path_str)
            basename = remote_path.name

            # 1. Match current repository (e.g. agent-manager)
            if basename == REPO_DIR.name:
                local_repo_uri = REPO_DIR.as_uri()
                if local_repo_uri not in adapted_uris:
                    adapted_uris.insert(0, local_repo_uri)

            # 2. Check if the subpath relative to user home exists locally
            parts = remote_path.parts
            for i in range(1, len(parts)):
                subpath = Path(*parts[i:])
                cand = home / subpath
                if cand.exists() and cand.is_dir():
                    cand_uri = cand.as_uri()
                    if cand_uri not in adapted_uris:
                        adapted_uris.insert(0, cand_uri)
                    break

        return json.dumps(adapted_uris)
    except Exception:
        return ws_raw


def import_sessions(
    session_ids: Optional[List[str]] = None,
    force: bool = False
) -> Dict[str, Any]:
    """
    Import sessions from the repository sync folder (SYNC_DIR) into the local Antigravity environment.
    Updates SQLite conversation_summaries and copies brain directory.
    """
    if not SYNC_DIR.exists():
        return {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    stats = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    # Ensure local DBs exist
    init_db_if_needed(APP_CONV_DB)
    init_db_if_needed(CLI_CONV_DB)

    for entry in SYNC_DIR.iterdir():
        if not entry.is_dir():
            continue
        meta_file = entry / "meta.json"
        if not meta_file.exists():
            continue

        cid = entry.name
        if session_ids and cid not in session_ids and not any(cid.startswith(s) for s in session_ids):
            continue

        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)

            remote_mtime = meta.get("last_modified_time")
            remote_dt = parse_sqlite_timestamp(remote_mtime)

            # Check local status across both DBs
            local_exists = False
            local_newer = False

            for db_path in [APP_CONV_DB, CLI_CONV_DB]:
                if not db_path.exists():
                    continue
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
                    cur = conn.cursor()
                    cur.execute("SELECT last_modified_time FROM conversation_summaries WHERE conversation_id = ?", (cid,))
                    row = cur.fetchone()
                    conn.close()
                    if row:
                        local_exists = True
                        local_dt = parse_sqlite_timestamp(row[0])
                        if local_dt and remote_dt and local_dt >= remote_dt:
                            local_newer = True
                except Exception:
                    pass

            if local_newer and not force:
                stats["skipped"] += 1
                continue

            # Adapt workspace URIs so the local Antigravity App matches the open folder
            if "workspace_uris" in meta:
                meta["workspace_uris"] = adapt_workspace_uris_for_local_machine(meta["workspace_uris"])

            # Normalize running status to IDLE for imported sessions
            if meta.get("status") == "CASCADE_RUN_STATUS_RUNNING":
                meta["status"] = "IDLE"
            meta["not_fully_idle"] = 0

            # Determine which DB to write to (write to both if both parent dirs exist, or appropriate one)
            dbs_to_write = []
            if CLI_CONV_DB.parent.exists():
                dbs_to_write.append(CLI_CONV_DB)
            if APP_CONV_DB.parent.exists() and APP_CONV_DB not in dbs_to_write:
                dbs_to_write.append(APP_CONV_DB)

            cols = [c for c in DB_COLUMNS if c in meta]
            col_names_str = ", ".join([f"`{c}`" for c in cols])
            placeholders = ", ".join(["?"] * len(cols))
            update_clauses = ", ".join([f"`{c}` = excluded.`{c}`" for c in cols if c != "conversation_id"])
            values = [meta[c] for c in cols]

            upsert_sql = f"""
            INSERT INTO conversation_summaries ({col_names_str})
            VALUES ({placeholders})
            ON CONFLICT(`conversation_id`) DO UPDATE SET {update_clauses};
            """

            for db_path in dbs_to_write:
                try:
                    conn = sqlite3.connect(db_path, timeout=5.0)
                    conn.execute(upsert_sql, values)
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

            # Sync brain folder
            brain_src = entry / "brain"
            if brain_src.exists():
                # Target brain directories
                targets = []
                app_data = meta.get("app_data_dir") or "antigravity"
                if app_data == "antigravity-cli" or (GEMINI_HOME / "antigravity-cli").exists():
                    targets.append(GEMINI_HOME / "antigravity-cli" / "brain" / cid)
                if app_data == "antigravity" or (GEMINI_HOME / "antigravity").exists():
                    targets.append(GEMINI_HOME / "antigravity" / "brain" / cid)

                for t in targets:
                    copy_brain_tree(brain_src, t)

            if local_exists:
                stats["updated"] += 1
            else:
                stats["imported"] += 1

        except Exception:
            stats["errors"] += 1

    return stats


def sync_all(limit: int = 50, force: bool = False) -> Dict[str, Any]:
    """
    Full 2-way synchronization:
    1. Import newer/missing sessions from SYNC_DIR (coming from other devices via Syncthing).
    2. Export local sessions to SYNC_DIR (pushing updates to other devices).
    """
    import_stats = import_sessions(force=force)
    export_stats = export_sessions(limit=limit, force=force)

    return {
        "imported": import_stats["imported"],
        "updated": import_stats["updated"],
        "exported": export_stats["exported"],
        "skipped": export_stats["skipped"],
        "errors": import_stats["errors"] + export_stats["errors"]
    }


def auto_import_synced_sessions():
    """
    Lightweight, silent auto-import of newly received Syncthing sessions.
    Called automatically when listing or resuming sessions.
    """
    try:
        if not SYNC_DIR.exists():
            return
        # Run import silently
        import_sessions()
    except Exception:
        pass


def get_sync_status() -> Dict[str, Any]:
    """
    Inspect the synchronization status between local machine and the Syncthing sync folder.
    """
    synced_sessions = {}
    if SYNC_DIR.exists():
        for d in SYNC_DIR.iterdir():
            if d.is_dir() and (d / "meta.json").exists():
                try:
                    with open(d / "meta.json", "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    dt = parse_sqlite_timestamp(meta.get("last_modified_time"))
                    synced_sessions[d.name] = {
                        "meta": meta,
                        "dt": dt,
                        "by": meta.get("_sync_exported_by", "Unknown"),
                        "at": meta.get("_sync_exported_at", "")
                    }
                except Exception:
                    pass

    # Read local sessions
    local_sessions = {}
    for db_path in [APP_CONV_DB, CLI_CONV_DB]:
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT conversation_id, title, preview, last_modified_time, workspace_uris FROM conversation_summaries")
            for r in cur.fetchall():
                cid = r["conversation_id"]
                dt = parse_sqlite_timestamp(r["last_modified_time"])
                if cid not in local_sessions or (dt and local_sessions[cid]["dt"] and dt > local_sessions[cid]["dt"]):
                    local_sessions[cid] = {
                        "row": dict(r),
                        "dt": dt
                    }
            conn.close()
        except Exception:
            pass

    all_ids = set(synced_sessions.keys()) | set(local_sessions.keys())
    items = []
    pending_pull = 0
    pending_push = 0
    in_sync = 0

    for cid in all_ids:
        in_local = cid in local_sessions
        in_remote = cid in synced_sessions

        title = ""
        ws = ""
        modified_str = "N/A"
        state = ""

        if in_local and not in_remote:
            state = "Chờ đẩy lên (Local only)"
            pending_push += 1
            dt = local_sessions[cid]["dt"]
            title = local_sessions[cid]["row"]["title"]
            ws_raw = local_sessions[cid]["row"]["workspace_uris"]
            try:
                uris = json.loads(ws_raw)
                if uris:
                    ws = Path(uris[0].replace("file://", "")).name
            except Exception:
                pass
            modified_str = format_relative_time(dt)

        elif not in_local and in_remote:
            state = "Chờ kéo về (Từ thiết bị khác)"
            pending_pull += 1
            dt = synced_sessions[cid]["dt"]
            title = synced_sessions[cid]["meta"].get("title", "")
            ws_raw = synced_sessions[cid]["meta"].get("workspace_uris", "")
            try:
                uris = json.loads(ws_raw)
                if uris:
                    ws = Path(uris[0].replace("file://", "")).name
            except Exception:
                pass
            modified_str = format_relative_time(dt)

        else:
            local_dt = local_sessions[cid]["dt"]
            remote_dt = synced_sessions[cid]["dt"]
            title = local_sessions[cid]["row"]["title"] or synced_sessions[cid]["meta"].get("title", "")
            ws_raw = local_sessions[cid]["row"]["workspace_uris"]
            try:
                uris = json.loads(ws_raw)
                if uris:
                    ws = Path(uris[0].replace("file://", "")).name
            except Exception:
                pass

            if remote_dt and local_dt and remote_dt > local_dt:
                state = "Có bản mới hơn từ thiết bị khác"
                pending_pull += 1
                modified_str = format_relative_time(remote_dt)
            elif remote_dt and local_dt and local_dt > remote_dt:
                state = "Bản local mới hơn (chờ đẩy)"
                pending_push += 1
                modified_str = format_relative_time(local_dt)
            else:
                state = "Đã đồng bộ"
                in_sync += 1
                modified_str = format_relative_time(local_dt)

        items.append({
            "id": cid,
            "title": title or "Không có tiêu đề",
            "workspace": ws,
            "state": state,
            "modified_str": modified_str,
            "dt": local_sessions.get(cid, {}).get("dt") or synced_sessions.get(cid, {}).get("dt") or datetime.min.replace(tzinfo=timezone.utc)
        })

    items.sort(key=lambda x: x["dt"], reverse=True)

    return {
        "sync_dir": str(SYNC_DIR),
        "total_synced": len(synced_sessions),
        "total_local": len(local_sessions),
        "pending_pull": pending_pull,
        "pending_push": pending_push,
        "in_sync": in_sync,
        "items": items[:25]
    }
