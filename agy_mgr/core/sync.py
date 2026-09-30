import base64
import json
import os
import shutil
import socket
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from agy_mgr.config import (
    APP_CONV_DB,
    CLI_CONV_DB,
    GEMINI_HOME,
    REPO_DIR,
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


def find_local_conversation_db(conversation_id: str) -> Optional[Path]:
    """Find local conversation SQLite DB for a given conversation_id."""
    candidates = [
        GEMINI_HOME / "antigravity" / "conversations" / f"{conversation_id}.db",
        GEMINI_HOME / "antigravity-cli" / "conversations" / f"{conversation_id}.db",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def find_local_annotation(conversation_id: str) -> Optional[Path]:
    """Find local annotation pbtxt for a given conversation_id."""
    candidates = [
        GEMINI_HOME / "antigravity" / "annotations" / f"{conversation_id}.pbtxt",
        GEMINI_HOME / "antigravity-cli" / "annotations" / f"{conversation_id}.pbtxt",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
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


def get_local_projects_map() -> Dict[str, Dict[str, Any]]:
    """
    Read all local projects from ~/.gemini/config/projects/*.json and ~/.gemini/projects.json.
    Maps lowercase project names and folder names to project info:
    {"id": pid, "name": name, "folderUri": folderUri}
    """
    projects = {}
    p_dir = GEMINI_HOME / "config" / "projects"
    if p_dir.exists():
        for f in p_dir.glob("*.json"):
            if f.name in ("outside-of-project.json", "default-cli-project.json"):
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                pid = d.get("id")
                pname = d.get("name")
                resources = d.get("projectResources", {}).get("resources", [])
                furi = None
                for r in resources:
                    if "gitFolder" in r and "folderUri" in r["gitFolder"]:
                        furi = r["gitFolder"]["folderUri"]
                        break
                if pid and pname:
                    entry = {"id": pid, "name": pname, "folderUri": furi}
                    projects[pname.lower()] = entry
                if pid and furi:
                    folder_name = Path(furi.replace("file://", "")).name.lower()
                    entry = {"id": pid, "name": pname or folder_name, "folderUri": furi}
                    projects[folder_name] = entry
            except Exception:
                pass

    return projects


def find_local_workspace_folder(ws_raw: str) -> Optional[Path]:
    """
    Given a remote workspace URI, find the corresponding local folder on this machine.
    """
    if not ws_raw:
        return None
    try:
        uris = json.loads(ws_raw)
        if not uris or not isinstance(uris, list):
            return None
        remote_path = Path(uris[0].replace("file://", ""))
        folder_name = remote_path.name

        # 1. Is it this repository itself?
        if folder_name.lower() == REPO_DIR.name.lower():
            return REPO_DIR

        # 2. Check if relative path from home exists on local machine
        home = Path.home()
        parts = remote_path.parts
        for i in range(1, len(parts)):
            subpath = Path(*parts[i:])
            cand = home / subpath
            if cand.exists() and cand.is_dir():
                return cand

        # 3. Check common search locations in user's home
        common_bases = [
            home / "Project",
            home / "Projects",
            home / "Documents",
            home / "workspace",
            home / "code",
            home,
        ]
        for base in common_bases:
            if not base.exists():
                continue
            direct = base / folder_name
            if direct.exists() and direct.is_dir():
                return direct
            try:
                for sub in base.iterdir():
                    if sub.is_dir():
                        target = sub / folder_name
                        if target.exists() and target.is_dir():
                            return target
            except Exception:
                pass
    except Exception:
        pass
    return None


def ensure_local_project_for_workspace(folder_path: Path) -> Dict[str, Any]:
    """
    Ensure a project JSON exists in ~/.gemini/config/projects/ for the given local folder.
    Returns {"id": pid, "name": name, "folderUri": folderUri}.
    """
    p_dir = GEMINI_HOME / "config" / "projects"
    p_dir.mkdir(parents=True, exist_ok=True)

    folder_uri = folder_path.as_uri()
    folder_name = folder_path.name

    # Check if project already exists
    for f in p_dir.glob("*.json"):
        if f.name in ("outside-of-project.json", "default-cli-project.json"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            for r in d.get("projectResources", {}).get("resources", []):
                if r.get("gitFolder", {}).get("folderUri") == folder_uri:
                    return {"id": d["id"], "name": d.get("name", folder_name), "folderUri": folder_uri}
        except Exception:
            pass

    # Create new project config
    new_pid = str(uuid.uuid4())
    proj_config = {
        "id": new_pid,
        "name": folder_name,
        "projectResources": {
            "resources": [
                {
                    "gitFolder": {
                        "folderUri": folder_uri,
                        "defaultBranch": "main"
                    }
                }
            ]
        },
        "permissionGrants": {
            "v2Migrated": True
        },
        "settings": {},
        "isWorkspaceOnly": False
    }
    target_file = p_dir / f"{new_pid}.json"
    target_file.write_text(json.dumps(proj_config, indent=2, ensure_ascii=False), encoding="utf-8")

    # Also update ~/.gemini/projects.json
    p_json = GEMINI_HOME / "projects.json"
    try:
        pj_data = {"projects": {}}
        if p_json.exists():
            pj_data = json.loads(p_json.read_text(encoding="utf-8"))
        pj_data.setdefault("projects", {})[str(folder_path)] = folder_name
        p_json.write_text(json.dumps(pj_data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    return {"id": new_pid, "name": folder_name, "folderUri": folder_uri}


def relink_sessions_to_local_projects() -> Dict[str, Any]:
    """
    Retroactively scan all sessions in local SQLite DBs and ensure their
    workspace_uris and project_id match the current machine's paths and projects.
    This guarantees Antigravity Desktop App displays sessions in the matching open workspace.
    """
    relinked_count = 0
    projects_map = get_local_projects_map()

    for db_path in [APP_CONV_DB, CLI_CONV_DB]:
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT conversation_id, workspace_uris, project_id, raw_summary FROM conversation_summaries")
            rows = cur.fetchall()

            for r in rows:
                cid = r["conversation_id"]
                ws_raw = r["workspace_uris"]
                old_pid = r["project_id"] or ""
                raw_blob = r["raw_summary"]

                local_folder = find_local_workspace_folder(ws_raw)
                if not local_folder:
                    continue

                folder_name_lower = local_folder.name.lower()
                target_proj = projects_map.get(folder_name_lower)
                if not target_proj or not target_proj.get("id") or not target_proj.get("folderUri"):
                    target_proj = ensure_local_project_for_workspace(local_folder)
                    projects_map[folder_name_lower] = target_proj

                target_pid = target_proj["id"]
                target_uri = target_proj["folderUri"]
                target_ws_raw = json.dumps([target_uri])

                need_update = False
                if old_pid != target_pid or ws_raw != target_ws_raw:
                    need_update = True

                new_raw = raw_blob
                if raw_blob and old_pid and target_pid and old_pid != target_pid:
                    if old_pid.encode() in raw_blob:
                        # 36-character UUID replacement preserves exact protobuf message length
                        new_raw = raw_blob.replace(old_pid.encode(), target_pid.encode())
                        need_update = True

                if need_update:
                    cur.execute(
                        "UPDATE conversation_summaries SET project_id = ?, workspace_uris = ?, raw_summary = ? WHERE conversation_id = ?",
                        (target_pid, target_ws_raw, new_raw, cid)
                    )
                    relinked_count += 1

            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(FULL);")
            conn.close()
        except Exception:
            pass

    return {"relinked": relinked_count}


def export_sessions(
    session_ids: Optional[List[str]] = None,
    limit: int = 50,
    force: bool = False
) -> Dict[str, Any]:
    """
    Export local sessions, conversation DBs, annotations, and brain data
    into the repository sync folder (SYNC_DIR).
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
            query = f"SELECT {cols_str}, raw_summary FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT ?"
            cursor.execute(query, (limit if not session_ids else 500,))
            for r in cursor.fetchall():
                cid = r["conversation_id"]
                if session_ids and cid not in session_ids and not any(cid.startswith(s) for s in session_ids):
                    continue
                dt = parse_sqlite_timestamp(r["last_modified_time"])
                row_d = dict(r)
                raw_blob = row_d.pop("raw_summary", None)
                if cid not in rows_by_id:
                    rows_by_id[cid] = (row_d, raw_blob, dt)
                else:
                    existing_dt = rows_by_id[cid][2]
                    if dt and existing_dt and dt > existing_dt:
                        rows_by_id[cid] = (row_d, raw_blob, dt)
            conn.close()
        except Exception:
            pass

    for cid, (row_dict, raw_blob, local_dt) in rows_by_id.items():
        try:
            sess_sync_dir = SYNC_DIR / cid
            meta_file = sess_sync_dir / "meta.json"

            # Check if sync directory already has an equal or newer version
            if meta_file.exists() and not force:
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta_data = json.load(f)
                    remote_dt = parse_sqlite_timestamp(meta_data.get("last_modified_time"))
                    # If remote is newer and conversation.db exists in sync, we can skip
                    if remote_dt and local_dt and remote_dt >= local_dt and (sess_sync_dir / "conversation.db").exists():
                        stats["skipped"] += 1
                        continue
                except Exception:
                    pass

            sess_sync_dir.mkdir(parents=True, exist_ok=True)

            # 1. Copy conversation SQLite DB (with WAL checkpoint)
            conv_db_src = find_local_conversation_db(cid)
            if conv_db_src:
                try:
                    chk_conn = sqlite3.connect(f"file:{conv_db_src}?mode=rw", uri=True, timeout=5.0)
                    chk_conn.execute("PRAGMA wal_checkpoint(FULL);")
                    chk_conn.close()
                except Exception:
                    pass
                shutil.copy2(conv_db_src, sess_sync_dir / "conversation.db")

            # 2. Copy annotation pbtxt
            ann_src = find_local_annotation(cid)
            if ann_src:
                shutil.copy2(ann_src, sess_sync_dir / "annotation.pbtxt")

            # 3. Copy brain directory
            brain_src = find_local_brain_dir(cid, row_dict.get("app_data_dir"))
            if brain_src:
                brain_dst = sess_sync_dir / "brain"
                copy_brain_tree(brain_src, brain_dst)

            # 4. Prepare and save meta.json
            meta_to_save = dict(row_dict)
            if raw_blob:
                try:
                    meta_to_save["_raw_summary_b64"] = base64.b64encode(raw_blob).decode("ascii")
                except Exception:
                    pass
            meta_to_save["_sync_exported_at"] = now_iso
            meta_to_save["_sync_exported_by"] = hostname

            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta_to_save, f, indent=2, ensure_ascii=False)

            stats["exported"] += 1
        except Exception:
            stats["errors"] += 1

    return stats


def import_sessions(
    session_ids: Optional[List[str]] = None,
    force: bool = False
) -> Dict[str, Any]:
    """
    Import sessions from the repository sync folder (SYNC_DIR) into the local Antigravity environment.
    Updates SQLite conversation_summaries, conversation.db, annotations, and brain directories.
    Automatically relinks workspace URIs and project_id to the local machine.
    """
    if not SYNC_DIR.exists():
        return {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    stats = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    # Ensure local DBs exist
    init_db_if_needed(APP_CONV_DB)
    init_db_if_needed(CLI_CONV_DB)

    projects_map = get_local_projects_map()

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
                        conv_db_local = find_local_conversation_db(cid)
                        if local_dt and remote_dt and local_dt >= remote_dt and conv_db_local:
                            local_newer = True
                except Exception:
                    pass

            if local_newer and not force:
                stats["skipped"] += 1
                continue

            # 1. Copy conversation.db to conversations/ folders
            conv_db_src = entry / "conversation.db"
            if conv_db_src.exists():
                for base in [GEMINI_HOME / "antigravity", GEMINI_HOME / "antigravity-cli"]:
                    if base.exists():
                        conv_dir = base / "conversations"
                        conv_dir.mkdir(parents=True, exist_ok=True)
                        dest_db = conv_dir / f"{cid}.db"
                        shutil.copy2(conv_db_src, dest_db)
                        (conv_dir / f"{cid}.db-wal").unlink(missing_ok=True)
                        (conv_dir / f"{cid}.db-shm").unlink(missing_ok=True)

            # 2. Copy annotation.pbtxt to annotations/ folders
            ann_src = entry / "annotation.pbtxt"
            if ann_src.exists():
                for base in [GEMINI_HOME / "antigravity", GEMINI_HOME / "antigravity-cli"]:
                    if base.exists():
                        ann_dir = base / "annotations"
                        ann_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(ann_src, ann_dir / f"{cid}.pbtxt")

            # 3. Match and adapt workspace URI and project_id for local machine
            ws_raw = meta.get("workspace_uris", "")
            old_pid = meta.get("project_id", "")
            raw_bytes = None
            if "_raw_summary_b64" in meta and meta["_raw_summary_b64"]:
                try:
                    raw_bytes = base64.b64decode(meta["_raw_summary_b64"])
                except Exception:
                    pass

            local_folder = find_local_workspace_folder(ws_raw)
            if local_folder:
                folder_name_lower = local_folder.name.lower()
                target_proj = projects_map.get(folder_name_lower)
                if not target_proj or not target_proj.get("id") or not target_proj.get("folderUri"):
                    target_proj = ensure_local_project_for_workspace(local_folder)
                    projects_map[folder_name_lower] = target_proj

                meta["project_id"] = target_proj["id"]
                meta["workspace_uris"] = json.dumps([target_proj["folderUri"]])

                # Replace old project_id with local project_id in raw_summary blob
                if raw_bytes and old_pid and target_proj["id"] and old_pid != target_proj["id"]:
                    if old_pid.encode() in raw_bytes:
                        raw_bytes = raw_bytes.replace(old_pid.encode(), target_proj["id"].encode())

            # Normalize running status to IDLE for imported sessions
            if meta.get("status") == "CASCADE_RUN_STATUS_RUNNING":
                meta["status"] = "IDLE"
            meta["not_fully_idle"] = 0

            # Determine which DB to write to
            dbs_to_write = []
            if CLI_CONV_DB.parent.exists():
                dbs_to_write.append(CLI_CONV_DB)
            if APP_CONV_DB.parent.exists() and APP_CONV_DB not in dbs_to_write:
                dbs_to_write.append(APP_CONV_DB)

            cols = [c for c in DB_COLUMNS if c in meta]
            values = [meta[c] for c in cols]

            if raw_bytes:
                cols.append("raw_summary")
                values.append(sqlite3.Binary(raw_bytes))

            col_names_str = ", ".join([f"`{c}`" for c in cols])
            placeholders = ", ".join(["?"] * len(cols))
            update_clauses = ", ".join([f"`{c}` = excluded.`{c}`" for c in cols if c != "conversation_id"])

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
                    conn.execute("PRAGMA wal_checkpoint(FULL);")
                    conn.close()
                except Exception:
                    pass

            # 4. Sync brain folder
            brain_src = entry / "brain"
            if brain_src.exists():
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

    # After importing, run retroactive relink across all sessions in local DB
    relink_sessions_to_local_projects()

    return stats


def sync_all(limit: int = 50, force: bool = False) -> Dict[str, Any]:
    """
    Full 2-way synchronization:
    1. Import newer/missing sessions from SYNC_DIR (coming from other devices via Syncthing).
    2. Export local sessions to SYNC_DIR (pushing updates to other devices).
    3. Relink sessions to local project paths.
    """
    import_stats = import_sessions(force=force)
    export_stats = export_sessions(limit=limit, force=force)
    relink_stats = relink_sessions_to_local_projects()

    return {
        "imported": import_stats["imported"],
        "updated": import_stats["updated"],
        "exported": export_stats["exported"],
        "skipped": export_stats["skipped"],
        "relinked": relink_stats["relinked"],
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
