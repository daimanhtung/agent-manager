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


def decode_proto(data: bytes) -> List[Any]:
    """Decode protobuf wire format into a recursive tree of (tag, wire, val)."""
    fields = []
    i = 0
    n = len(data)
    while i < n:
        key = 0
        shift = 0
        while True:
            if i >= n:
                return fields
            b = data[i]
            i += 1
            key |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        tag = key >> 3
        wire = key & 0x07
        if wire == 0:
            val = 0
            shift = 0
            while True:
                if i >= n:
                    return fields
                b = data[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields.append((tag, wire, val))
        elif wire == 1:
            if i + 8 > n:
                return fields
            val = data[i:i + 8]
            i += 8
            fields.append((tag, wire, val))
        elif wire == 2:
            length = 0
            shift = 0
            while True:
                if i >= n:
                    return fields
                b = data[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if i + length > n:
                return fields
            val = data[i:i + length]
            i += length
            try:
                sub = decode_proto(val)
                if encode_proto(sub) == val:
                    fields.append((tag, wire, sub))
                    continue
            except Exception:
                pass
            fields.append((tag, wire, val))
        elif wire == 5:
            if i + 4 > n:
                return fields
            val = data[i:i + 4]
            i += 4
            fields.append((tag, wire, val))
        else:
            return fields
    return fields


def encode_varint(val: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    res = bytearray()
    while val > 0x7F:
        res.append((val & 0x7F) | 0x80)
        val >>= 7
    res.append(val & 0x7F)
    return bytes(res)


def encode_proto(fields: List[Any]) -> bytes:
    """Encode a parsed protobuf tree back into wire bytes."""
    out = bytearray()
    for tag, wire, val in fields:
        key = (tag << 3) | wire
        out.extend(encode_varint(key))
        if wire == 0:
            out.extend(encode_varint(val))
        elif wire == 1 or wire == 5:
            out.extend(val)
        elif wire == 2:
            if isinstance(val, list):
                sub_bytes = encode_proto(val)
                out.extend(encode_varint(len(sub_bytes)))
                out.extend(sub_bytes)
            elif isinstance(val, (bytes, bytearray)):
                out.extend(encode_varint(len(val)))
                out.extend(val)
            else:
                out.extend(encode_varint(0))
    return bytes(out)


def find_proto_uris(fields: List[Any]) -> List[bytes]:
    """Find all string fields in protobuf tree that look like file:/// URIs."""
    uris = set()
    for tag, wire, val in fields:
        if wire == 2:
            if isinstance(val, list):
                uris.update(find_proto_uris(val))
            elif isinstance(val, (bytes, bytearray)):
                try:
                    s = val.decode("utf-8")
                    if s.startswith("file:///"):
                        uris.add(val)
                except Exception:
                    pass
    return list(uris)


def replace_in_proto_tree(fields: List[Any], replacements: List[tuple]) -> List[Any]:
    """Recursively replace byte strings in length-delimited fields."""
    new_fields = []
    for tag, wire, val in fields:
        if wire == 2:
            if isinstance(val, list):
                new_fields.append((tag, wire, replace_in_proto_tree(val, replacements)))
            elif isinstance(val, (bytes, bytearray)):
                v = val
                for old_b, new_b in replacements:
                    if old_b and new_b and old_b in v:
                        v = v.replace(old_b, new_b)
                new_fields.append((tag, wire, v))
        else:
            new_fields.append((tag, wire, val))
    return new_fields


def patch_protobuf_blob(
    blob: Optional[bytes],
    old_pid: Optional[str] = None,
    new_pid: Optional[str] = None,
    new_uri: Optional[str] = None
) -> Optional[bytes]:
    """Safely patch project ID and workspace URIs inside a raw protobuf blob."""
    if not blob:
        return blob
    try:
        tree = decode_proto(blob)
        replacements = []
        if old_pid and new_pid and old_pid != new_pid:
            replacements.append((old_pid.encode("utf-8"), new_pid.encode("utf-8")))
        if new_uri:
            target_uri_b = new_uri.encode("utf-8")
            existing_uris = find_proto_uris(tree)
            for u in existing_uris:
                if u != target_uri_b:
                    replacements.append((u, target_uri_b))
        if not replacements:
            return blob
        new_tree = replace_in_proto_tree(tree, replacements)
        return encode_proto(new_tree)
    except Exception:
        if old_pid and new_pid and old_pid.encode() in blob:
            return blob.replace(old_pid.encode(), new_pid.encode())
        return blob


def patch_conversation_db(cid: str, new_uri: Optional[str] = None, new_pid: Optional[str] = None):
    """Patch trajectory_metadata_blob inside local conversation SQLite DB."""
    if not new_uri and not new_pid:
        return
    for base_dir in [GEMINI_HOME / "antigravity", GEMINI_HOME / "antigravity-cli"]:
        conv_file = base_dir / "conversations" / f"{cid}.db"
        if not conv_file.exists():
            continue
        try:
            conn = sqlite3.connect(conv_file, timeout=5.0)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_metadata_blob'")
            if cur.fetchone():
                cur.execute("SELECT id, data FROM trajectory_metadata_blob")
                rows = cur.fetchall()
                for row_id, blob in rows:
                    if blob:
                        btree = decode_proto(blob)
                        breps = []
                        if new_uri:
                            buris = find_proto_uris(btree)
                            target_uri_b = new_uri.encode("utf-8")
                            for u in buris:
                                if u != target_uri_b:
                                    breps.append((u, target_uri_b))
                        if new_pid:
                            target_pid_b = new_pid.encode("utf-8")
                            for tag, wire, val in btree:
                                if tag == 18 and wire == 2 and isinstance(val, (bytes, bytearray)):
                                    if bytes(val) != target_pid_b:
                                        breps.append((bytes(val), target_pid_b))
                        if breps:
                            patched = encode_proto(replace_in_proto_tree(btree, breps))
                            cur.execute("UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?", (patched, row_id))
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            conn.close()
        except Exception:
            pass


def synthesize_raw_summary(
    cid: str,
    title: Optional[str],
    preview: Optional[str],
    step_count: Optional[int],
    mtime_str: Optional[str],
    ws_raw: Optional[str],
    pid: str
) -> bytes:
    """Synthesize a valid protobuf raw_summary BLOB from conversation metadata."""
    text = (title or preview or "Untitled").encode("utf-8")
    dt = parse_sqlite_timestamp(mtime_str) or datetime.now(timezone.utc)
    ts = int(dt.timestamp())
    pid_b = pid.encode("utf-8") if pid else b""
    try:
        uris = json.loads(ws_raw) if ws_raw else []
        uri = uris[0] if uris else ""
    except Exception:
        uri = ""
    uri_b = uri.encode("utf-8")

    time_proto = [(1, 0, ts), (2, 0, 0)]
    tag17 = [
        (6, 2, cid.encode("utf-8")),
        (7, 2, uri_b),
        (18, 2, pid_b)
    ]
    if uri_b:
        tag17.insert(0, (1, 2, [(1, 2, uri_b)]))

    tag9 = []
    if uri_b:
        tag9.append((1, 2, uri_b))

    fields = [
        (1, 2, text),
        (2, 0, step_count or 1),
        (3, 2, time_proto),
        (4, 2, pid_b),
        (5, 0, 1),
        (7, 2, time_proto),
        (9, 2, tag9),
        (10, 2, time_proto),
        (17, 2, tag17),
        (22, 0, 4)
    ]
    return encode_proto(fields)


def synthesize_conversation_db(
    cid: str,
    project_id: str,
    workspace_uri: str,
    mtime_str: Optional[str] = None
) -> Optional[Path]:
    """
    Synthesize a valid SQLite conversation database for a session when the original .db file is missing.
    Prevents Antigravity Desktop App from silently deleting the session from its sidebar/cache.
    """
    dt = parse_sqlite_timestamp(mtime_str) or datetime.now(timezone.utc)
    ts = int(dt.timestamp())
    uri_b = workspace_uri.encode("utf-8") if workspace_uri else b""
    pid_b = project_id.encode("utf-8") if project_id else b""

    fields = [
        (1, 2, [(1, 2, uri_b), (3, 2, [])]),
        (2, 2, [(1, 0, ts), (2, 0, 0)]),
        (3, 2, str(uuid.uuid4()).encode("utf-8")),
        (7, 2, uri_b),
        (18, 2, pid_b)
    ]
    blob_bytes = encode_proto(fields)

    created_paths = []
    for base_dir in [GEMINI_HOME / "antigravity", GEMINI_HOME / "antigravity-cli"]:
        conv_dir = base_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        dest_db = conv_dir / f"{cid}.db"
        if not dest_db.exists():
            try:
                conn = sqlite3.connect(dest_db, timeout=5.0)
                conn.execute("CREATE TABLE IF NOT EXISTS `trajectory_meta` (`trajectory_id` text,`cascade_id` text,`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`));")
                conn.execute("CREATE TABLE IF NOT EXISTS `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,`metadata` blob,`error_details` blob,`permissions` blob,`task_details` blob,`render_info` blob,`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));")
                conn.execute("CREATE TABLE IF NOT EXISTS `gen_metadata` (`idx` integer,`data` blob,`size` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));")
                conn.execute("CREATE TABLE IF NOT EXISTS `executor_metadata` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));")
                conn.execute("CREATE TABLE IF NOT EXISTS `parent_references` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));")
                conn.execute("CREATE TABLE IF NOT EXISTS `trajectory_metadata_blob` (`id` text DEFAULT 'main',`data` blob,PRIMARY KEY (`id`));")
                conn.execute("CREATE TABLE IF NOT EXISTS `battle_mode_infos` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));")

                traj_id = str(uuid.uuid4())
                conn.execute("INSERT OR REPLACE INTO trajectory_meta VALUES (?, ?, 4, 17)", (traj_id, cid))
                conn.execute("INSERT OR REPLACE INTO trajectory_metadata_blob VALUES ('main', ?)", (blob_bytes,))
                conn.commit()
                conn.close()
                created_paths.append(dest_db)
            except Exception:
                pass

    # Also save to SYNC_DIR if missing so Syncthing pushes it to other machines
    sync_conv = SYNC_DIR / cid / "conversation.db"
    if not sync_conv.exists() and created_paths:
        try:
            (SYNC_DIR / cid).mkdir(parents=True, exist_ok=True)
            shutil.copy2(created_paths[0], sync_conv)
        except Exception:
            pass

    return created_paths[0] if created_paths else None


def ensure_all_conversation_dbs() -> int:
    """
    Ensure every session in conversation_summaries.db has a corresponding conversation SQLite DB file.
    If missing, automatically synthesizes it to ensure Antigravity Desktop App renders it in the sidebar.
    Returns the count of newly synthesized databases.
    """
    synthesized = 0
    for db_path in [APP_CONV_DB, CLI_CONV_DB]:
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT conversation_id, project_id, workspace_uris, last_modified_time FROM conversation_summaries")
            rows = cur.fetchall()
            conn.close()

            for r in rows:
                cid = r["conversation_id"]
                conv_file = find_local_conversation_db(cid)
                if not conv_file:
                    ws_raw = r["workspace_uris"]
                    uri = ""
                    if ws_raw:
                        try:
                            uris = json.loads(ws_raw)
                            if uris:
                                uri = uris[0]
                        except Exception:
                            pass
                    res = synthesize_conversation_db(
                        cid=cid,
                        project_id=r["project_id"] or "",
                        workspace_uri=uri,
                        mtime_str=r["last_modified_time"]
                    )
                    if res:
                        synthesized += 1
        except Exception:
            pass
    return synthesized


def patch_agyhub_summaries_pb(projects_map: Dict[str, Dict[str, Any]]):
    """Patch agyhub_summaries_proto.pb to map all foreign URIs and project IDs to local ones, and add missing sessions."""
    pb_file = GEMINI_HOME / "antigravity" / "agyhub_summaries_proto.pb"
    if not pb_file.exists():
        return
    try:
        db_map = {}
        for db_path in [APP_CONV_DB, CLI_CONV_DB]:
            if db_path.exists():
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
                    cur = conn.cursor()
                    cur.execute("SELECT conversation_id, project_id, workspace_uris, raw_summary, title, preview, step_count, last_modified_time FROM conversation_summaries")
                    for r in cur.fetchall():
                        cid = r[0]
                        pid = r[1]
                        ws = r[2]
                        raw_blob = r[3]
                        title_val = r[4]
                        preview_val = r[5]
                        clean_title = (title_val or preview_val or "Untitled Conversation").strip()
                        if cid and pid:
                            uris = json.loads(ws) if ws else []
                            uri = uris[0] if uris else None
                            if not raw_blob or len(raw_blob) == 0:
                                raw_blob = synthesize_raw_summary(cid, clean_title, preview_val, r[6], r[7], ws, pid)
                            db_map[cid] = {
                                "pid": pid,
                                "uri": uri,
                                "raw": raw_blob,
                                "title": clean_title
                            }
                    conn.close()
                except Exception:
                    pass

        pb_data = pb_file.read_bytes()
        tree = decode_proto(pb_data)

        seen_cids = set()

        def patch_item(item):
            if item[0] != 1 or not isinstance(item[2], list):
                return item
            cid = None
            for sub in item[2]:
                if sub[0] == 1 and isinstance(sub[2], bytes):
                    cid = sub[2].decode(errors="ignore")
                    break

            target_pid = None
            target_uri = None
            target_title = "Untitled Conversation"
            if cid and cid in db_map:
                seen_cids.add(cid)
                target_pid = db_map[cid]["pid"]
                target_uri = db_map[cid]["uri"]
                target_title = db_map[cid]["title"]
            else:
                return None

            target_pid_b = target_pid.encode("utf-8") if target_pid else None
            target_uri_b = target_uri.encode("utf-8") if target_uri else None
            target_title_b = target_title.encode("utf-8")

            def patch_tag2(sub2_list):
                new_sub2 = []
                has_tag1 = False
                for s in sub2_list:
                    tag, wire, val = s
                    if tag == 1:
                        has_tag1 = True
                        new_sub2.append((1, 2, target_title_b))
                    elif tag == 4 and isinstance(val, (bytes, bytearray)) and target_pid_b:
                        new_sub2.append((tag, wire, target_pid_b))
                    elif tag == 17 and isinstance(val, list):
                        new_17 = []
                        for s17 in val:
                            t17, w17, v17 = s17
                            if t17 == 18 and isinstance(v17, (bytes, bytearray)) and target_pid_b:
                                new_17.append((t17, w17, target_pid_b))
                            elif t17 in (1, 7) and isinstance(v17, (bytes, bytearray)) and v17.startswith(b"file://"):
                                uri_to_use = target_uri_b
                                if not uri_to_use:
                                    fname = Path(v17.decode("utf-8", errors="ignore").replace("file://", "")).name.lower()
                                    if fname in projects_map and projects_map[fname].get("folderUri"):
                                        uri_to_use = projects_map[fname]["folderUri"].encode("utf-8")
                                new_17.append((t17, w17, uri_to_use or v17))
                            elif t17 == 1 and isinstance(v17, list):
                                new_sub1 = []
                                for n1 in v17:
                                    if n1[0] in (1, 2) and isinstance(n1[2], (bytes, bytearray)) and n1[2].startswith(b"file://"):
                                        uri_to_use = target_uri_b
                                        if not uri_to_use:
                                            fname = Path(n1[2].decode("utf-8", errors="ignore").replace("file://", "")).name.lower()
                                            if fname in projects_map and projects_map[fname].get("folderUri"):
                                                uri_to_use = projects_map[fname]["folderUri"].encode("utf-8")
                                        new_sub1.append((n1[0], n1[1], uri_to_use or n1[2]))
                                    else:
                                        new_sub1.append(n1)
                                new_17.append((t17, w17, new_sub1))
                            else:
                                new_17.append(s17)
                        new_sub2.append((tag, wire, new_17))
                    elif tag == 9 and isinstance(val, list):
                        new_9 = []
                        for s9 in val:
                            t9, w9, v9 = s9
                            if t9 in (1, 2) and isinstance(v9, (bytes, bytearray)) and v9.startswith(b"file://"):
                                uri_to_use = target_uri_b
                                if not uri_to_use:
                                    fname = Path(v9.decode("utf-8", errors="ignore").replace("file://", "")).name.lower()
                                    if fname in projects_map and projects_map[fname].get("folderUri"):
                                        uri_to_use = projects_map[fname]["folderUri"].encode("utf-8")
                                new_9.append((t9, w9, uri_to_use or v9))
                            else:
                                new_9.append(s9)
                        new_sub2.append((tag, wire, new_9))
                    else:
                        new_sub2.append(s)

                if not has_tag1:
                    new_sub2.insert(0, (1, 2, target_title_b))

                return new_sub2

            new_subs = []
            for sub in item[2]:
                tag, wire, val = sub
                if tag == 2 and isinstance(val, list):
                    new_subs.append((tag, wire, patch_tag2(val)))
                else:
                    new_subs.append(sub)
            return (item[0], item[1], new_subs)

        new_tree = [patch_item(it) for it in tree]
        new_tree = [it for it in new_tree if it is not None]

        # Add missing sessions from db_map into pb cache
        for cid, info in db_map.items():
            if cid not in seen_cids and info.get("raw"):
                try:
                    raw_tree = decode_proto(info["raw"])
                    has_t1 = any(t[0] == 1 for t in raw_tree)
                    if not has_t1 and info.get("title"):
                        raw_tree.insert(0, (1, 2, info["title"].encode("utf-8")))
                    new_tree.append((1, 2, [
                        (1, 2, cid.encode("utf-8")),
                        (2, 2, raw_tree)
                    ]))
                except Exception:
                    pass

        new_pb = encode_proto(new_tree)
        pb_file.write_bytes(new_pb)
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
                if src_file.stat().st_size > 50 * 1024 * 1024:
                    continue
                # For transcript files, NEVER overwrite if destination has more or equal bytes!
                if f in ("transcript.jsonl", "transcript_full.jsonl") and dst_file.exists():
                    if dst_file.stat().st_size >= src_file.stat().st_size:
                        continue
                if not dst_file.exists() or src_file.stat().st_mtime > dst_file.stat().st_mtime:
                    shutil.copy2(src_file, dst_file)
            except Exception:
                pass


def get_local_projects_map() -> Dict[str, Dict[str, Any]]:
    """
    Read all local projects from ~/.gemini/config/projects/*.json.
    Maps lowercase project names and folder names to project info:
    {"id": pid, "name": name, "folderUri": folderUri, "file": Path}
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
                    entry = {"id": pid, "name": pname, "folderUri": furi, "file": f}
                    projects[pname.lower()] = entry
                if pid and furi:
                    folder_name = Path(furi.replace("file://", "")).name.lower()
                    entry = {"id": pid, "name": pname or folder_name, "folderUri": furi, "file": f}
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
    Ensure a single project JSON exists in ~/.gemini/config/projects/ for the given local folder.
    NEVER creates duplicates if a project with the same name already exists!
    """
    p_dir = GEMINI_HOME / "config" / "projects"
    p_dir.mkdir(parents=True, exist_ok=True)

    folder_uri = folder_path.as_uri()
    folder_name = folder_path.name
    folder_name_lower = folder_name.lower()

    # Check if a project with the same name or folderUri already exists
    for f in p_dir.glob("*.json"):
        if f.name in ("outside-of-project.json", "default-cli-project.json"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            pid = d.get("id")
            pname = d.get("name", "")

            # Check matching folderUri
            for r in d.get("projectResources", {}).get("resources", []):
                if r.get("gitFolder", {}).get("folderUri") == folder_uri:
                    return {"id": pid, "name": pname or folder_name, "folderUri": folder_uri, "file": f}

            # Check matching name
            if pname and pname.lower() == folder_name_lower:
                resources = d.get("projectResources", {}).get("resources", [])
                if not resources or not resources[0].get("gitFolder", {}).get("folderUri"):
                    d.setdefault("projectResources", {})["resources"] = [{
                        "gitFolder": {
                            "folderUri": folder_uri,
                            "defaultBranch": "main"
                        }
                    }]
                    f.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
                return {"id": pid, "name": pname, "folderUri": folder_uri, "file": f}
        except Exception:
            pass

    # Create new project config only if none exists
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

    return {"id": new_pid, "name": folder_name, "folderUri": folder_uri, "file": target_file}


def deduplicate_and_relink_projects() -> Dict[str, Any]:
    """
    Comprehensive fix for Antigravity Desktop App session display:
    1. Merges any duplicate projects in ~/.gemini/config/projects/ (e.g. multiple 'devops').
    2. Deletes duplicate project JSON files.
    3. Cleans up app_storage.json (removes duplicates, uncollapses project sections).
    4. Relinks all sessions in conversation_summaries.db to point to the primary project ID
       and the local machine's folder URI.
    5. Replaces old project UUID in raw_summary protobuf blobs.
    6. Checkpoints SQLite WAL.
    """
    p_dir = GEMINI_HOME / "config" / "projects"
    p_dir.mkdir(parents=True, exist_ok=True)

    # 0. Populate empty titles from preview if available
    for db_path in [APP_CONV_DB, CLI_CONV_DB]:
        if db_path.exists():
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                # Purge ghost sessions (0 steps and no title/preview)
                conn.execute("""
                    DELETE FROM conversation_summaries
                    WHERE (step_count = 0 OR step_count IS NULL)
                      AND (title = '' OR title IS NULL)
                      AND (preview = '' OR preview IS NULL);
                """)
                conn.execute('UPDATE conversation_summaries SET title = preview WHERE (title = "" OR title IS NULL) AND (preview != "" AND preview IS NOT NULL);')
                conn.commit()
                conn.close()
            except Exception:
                pass

    # 1. Pre-calculate session counts per project ID
    session_counts: Dict[str, int] = {}
    for db_path in [APP_CONV_DB, CLI_CONV_DB]:
        if db_path.exists():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
                cur = conn.cursor()
                cur.execute("SELECT project_id, COUNT(*) FROM conversation_summaries GROUP BY project_id")
                for row in cur.fetchall():
                    if row[0]:
                        session_counts[row[0]] = session_counts.get(row[0], 0) + row[1]
                conn.close()
            except Exception:
                pass

    # Group projects by lowercase name
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for f in p_dir.glob("*.json"):
        if f.name in ("outside-of-project.json", "default-cli-project.json"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            name = (d.get("name") or "").strip().lower()
            pid = d.get("id")
            if not name or not pid:
                continue

            resources = d.get("projectResources", {}).get("resources", [])
            furi = None
            for r in resources:
                if "gitFolder" in r and "folderUri" in r["gitFolder"]:
                    furi = r["gitFolder"]["folderUri"]
                    break

            sess_count = session_counts.get(pid, 0)

            by_name.setdefault(name, []).append({
                "file": f,
                "id": pid,
                "data": d,
                "folderUri": furi,
                "session_count": sess_count,
                "mtime": f.stat().st_mtime
            })
        except Exception:
            pass

    merged_ids: Dict[str, str] = {}
    primary_projects: Dict[str, Dict[str, Any]] = {}
    duplicates_removed = 0

    for name, items in by_name.items():
        if len(items) == 1:
            primary_projects[name] = items[0]
            continue

        # Sort: project with most sessions first, then oldest mtime
        items.sort(key=lambda x: (x["session_count"], -x["mtime"]), reverse=True)
        primary = items[0]
        primary_projects[name] = primary

        for dup in items[1:]:
            dup_id = dup["id"]
            merged_ids[dup_id] = primary["id"]
            try:
                dup["file"].unlink(missing_ok=True)
                duplicates_removed += 1
            except Exception:
                pass

    # Ensure each primary project has a valid local folderUri
    for name, proj in primary_projects.items():
        if not proj.get("folderUri"):
            local_folder = find_local_workspace_folder(json.dumps([f"file:///{name}"]))
            if local_folder:
                proj["folderUri"] = local_folder.as_uri()
                proj["data"].setdefault("projectResources", {})["resources"] = [{
                    "gitFolder": {
                        "folderUri": local_folder.as_uri(),
                        "defaultBranch": "main"
                    }
                }]
                try:
                    proj["file"].write_text(json.dumps(proj["data"], indent=2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

    # 2. Clean up ~/Library/Application Support/Antigravity/app_storage.json
    app_storage = Path.home() / "Library" / "Application Support" / "Antigravity" / "app_storage.json"
    if app_storage.exists():
        try:
            storage_data = json.loads(app_storage.read_text(encoding="utf-8"))
            primary_ids = {p["id"] for p in primary_projects.values()}

            if "projectsOrder" in storage_data:
                order = json.loads(storage_data["projectsOrder"])
                new_order = []
                for pid in order:
                    actual = merged_ids.get(pid, pid)
                    if actual not in new_order and (actual in primary_ids or (p_dir / f"{actual}.json").exists()):
                        new_order.append(actual)
                for pid in primary_ids:
                    if pid not in new_order:
                        new_order.append(pid)
                storage_data["projectsOrder"] = json.dumps(new_order)

            # Uncollapse sections so they are expanded and visible in the sidebar!
            if "sidebar_collapsed_sections" in storage_data:
                collapsed = json.loads(storage_data["sidebar_collapsed_sections"])
                new_collapsed = [
                    pid for pid in collapsed
                    if pid not in merged_ids and pid not in primary_ids
                ]
                storage_data["sidebar_collapsed_sections"] = json.dumps(new_collapsed)

            app_storage.write_text(json.dumps(storage_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # 3. Relink all sessions in SQLite DBs
    relinked_sessions = 0
    projects_map = get_local_projects_map()

    for db_path in [APP_CONV_DB, CLI_CONV_DB]:
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT conversation_id, workspace_uris, project_id, raw_summary, title, preview, step_count, last_modified_time FROM conversation_summaries")
            rows = cur.fetchall()

            for r in rows:
                cid = r["conversation_id"]
                ws_raw = r["workspace_uris"]
                curr_pid = r["project_id"] or ""
                raw_blob = r["raw_summary"]
                title_val = (r["title"] or "").strip()
                preview_val = (r["preview"] or "").strip()
                steps_val = r["step_count"]
                mtime_val = r["last_modified_time"]

                need_update = False

                # 1. Recover missing metadata from meta.json if title, preview, or workspace is missing
                if not title_val or not preview_val or not ws_raw or ws_raw == "[]":
                    mf = SYNC_DIR / cid / "meta.json"
                    if mf.exists():
                        try:
                            m = json.load(open(mf))
                            meta_t = (m.get("title") or "").strip()
                            meta_p = (m.get("preview") or "").strip()
                            meta_ws = m.get("workspace_uris", "")
                            if not title_val and meta_t:
                                title_val = meta_t
                                need_update = True
                            if not preview_val and meta_p:
                                preview_val = meta_p
                                need_update = True
                            if (not ws_raw or ws_raw == "[]") and meta_ws:
                                ws_raw = meta_ws
                                need_update = True
                            if not steps_val and m.get("step_count"):
                                steps_val = m.get("step_count")
                                need_update = True
                            if (not mtime_val or mtime_val.startswith("0001")) and m.get("last_modified_time") and not m.get("last_modified_time").startswith("0001"):
                                mtime_val = m.get("last_modified_time")
                                need_update = True
                        except Exception:
                            pass

                # 2. If title is still empty, fall back to preview!
                # Antigravity Desktop App ONLY renders the 'title' column; empty title shows 'Untitled Conversation'!
                if not title_val and preview_val:
                    title_val = preview_val
                    need_update = True

                # Extract folder name
                folder_name = ""
                if ws_raw:
                    try:
                        uris = json.loads(ws_raw)
                        if uris:
                            folder_name = Path(uris[0].replace("file://", "")).name.lower()
                    except Exception:
                        pass

                target_proj = projects_map.get(folder_name)
                if not target_proj and folder_name:
                    local_folder = find_local_workspace_folder(ws_raw)
                    if local_folder:
                        target_proj = ensure_local_project_for_workspace(local_folder)
                        projects_map[folder_name] = target_proj

                if not target_proj:
                    continue

                target_pid = target_proj["id"]
                target_uri = target_proj.get("folderUri") or (json.loads(ws_raw)[0] if ws_raw else "")
                target_ws_raw = json.dumps([target_uri])

                if curr_pid != target_pid or ws_raw != target_ws_raw:
                    need_update = True

                if not raw_blob or len(raw_blob) == 0:
                    raw_blob = synthesize_raw_summary(
                        cid=cid,
                        title=title_val,
                        preview=preview_val,
                        step_count=steps_val or 1,
                        mtime_str=mtime_val,
                        ws_raw=target_ws_raw,
                        pid=target_pid
                    )
                    need_update = True

                new_raw = patch_protobuf_blob(raw_blob, old_pid=curr_pid, new_pid=target_pid, new_uri=target_uri)
                if new_raw != raw_blob:
                    need_update = True

                if target_uri or target_pid:
                    patch_conversation_db(cid, new_uri=target_uri, new_pid=target_pid)

                if need_update:
                    cur.execute(
                        "UPDATE conversation_summaries SET title = ?, preview = ?, project_id = ?, workspace_uris = ?, raw_summary = ?, step_count = ?, last_modified_time = ? WHERE conversation_id = ?",
                        (title_val, preview_val, target_pid, target_ws_raw, new_raw, steps_val or 0, mtime_val or "0001-01-01 00:00:00+00:00", cid)
                    )
                    relinked_sessions += 1

            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            conn.close()
        except Exception:
            pass

    # 4. Patch agyhub_summaries_proto.pb if present
    patch_agyhub_summaries_pb(projects_map)

    return {
        "duplicates_removed": duplicates_removed,
        "relinked": relinked_sessions
    }


def relink_sessions_to_local_projects() -> Dict[str, Any]:
    """Alias for deduplicate_and_relink_projects."""
    return deduplicate_and_relink_projects()


def export_sessions(
    session_ids: Optional[List[str]] = None,
    limit: int = 0,
    force: bool = False,
    project: Optional[str] = None
) -> Dict[str, Any]:
    """
    Export local sessions, conversation DBs, annotations, and brain data
    into the repository sync folder (SYNC_DIR).
    """
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    hostname = socket.gethostname()
    now_iso = datetime.now(timezone.utc).isoformat()

    stats = {"exported": 0, "skipped": 0, "errors": 0}

    rows_by_id = {}
    for db_path in [APP_CONV_DB, CLI_CONV_DB]:
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cols_str = ", ".join([f"`{c}`" for c in DB_COLUMNS])
            query = f"SELECT {cols_str}, raw_summary FROM conversation_summaries ORDER BY last_modified_time DESC"
            if limit and limit > 0 and not session_ids:
                query += " LIMIT ?"
                cursor.execute(query, (limit,))
            else:
                cursor.execute(query)
            for r in cursor.fetchall():
                cid = r["conversation_id"]
                if session_ids and cid not in session_ids and not any(cid.startswith(s) for s in session_ids):
                    continue
                if project:
                    ws_raw = r["workspace_uris"] or ""
                    matched_project = False
                    try:
                        uris = json.loads(ws_raw)
                        for u in uris:
                            folder = Path(u.replace("file://", "")).name.lower()
                            if project.lower() == folder or project.lower() in u.lower():
                                matched_project = True
                                break
                    except Exception:
                        if project.lower() in ws_raw.lower():
                            matched_project = True
                    if not matched_project:
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
            # Skip ghost sessions (0 steps and no title/preview)
            step_cnt = row_dict.get("step_count", 0)
            t_val = (row_dict.get("title") or "").strip()
            p_val = (row_dict.get("preview") or "").strip()
            if (not step_cnt or step_cnt <= 0) and not t_val and not p_val:
                stats["skipped"] += 1
                continue

            # Standardize title and preview
            if not t_val and p_val:
                row_dict["title"] = p_val
            elif not p_val and t_val:
                row_dict["preview"] = t_val

            # Require real conversation data (conversation.db with steps > 0 or transcript)
            conv_db_src = find_local_conversation_db(cid)
            brain_src = find_local_brain_dir(cid, row_dict.get("app_data_dir"))
            has_transcript = brain_src and (brain_src / ".system_generated" / "logs" / "transcript.jsonl").exists()
            has_real_steps = False
            if conv_db_src:
                try:
                    c = sqlite3.connect(f"file:{conv_db_src}?mode=ro", uri=True, timeout=2.0)
                    has_real_steps = c.execute("SELECT COUNT(*) FROM steps").fetchone()[0] > 0
                    c.close()
                except Exception:
                    pass
            if not has_real_steps and not has_transcript:
                stats["skipped"] += 1
                continue

            sess_sync_dir = SYNC_DIR / cid
            meta_file = sess_sync_dir / "meta.json"

            if meta_file.exists() and not force:
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta_data = json.load(f)
                    remote_dt = parse_sqlite_timestamp(meta_data.get("last_modified_time"))
                    if remote_dt and local_dt and remote_dt >= local_dt and (sess_sync_dir / "conversation.db").exists():
                        stats["skipped"] += 1
                        continue
                except Exception:
                    pass

            sess_sync_dir.mkdir(parents=True, exist_ok=True)

            # 1. Copy conversation SQLite DB (with WAL checkpoint)
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
            if not raw_blob or len(raw_blob) == 0:
                try:
                    raw_blob = synthesize_raw_summary(
                        cid=cid,
                        title=row_dict.get("title"),
                        preview=row_dict.get("preview"),
                        step_count=row_dict.get("step_count"),
                        mtime_str=row_dict.get("last_modified_time"),
                        ws_raw=row_dict.get("workspace_uris"),
                        pid=row_dict.get("project_id", "")
                    )
                except Exception:
                    pass
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
    force: bool = False,
    project: Optional[str] = None
) -> Dict[str, Any]:
    """
    Import sessions from the repository sync folder (SYNC_DIR) into the local Antigravity environment.
    Updates SQLite conversation_summaries, conversation.db, annotations, and brain directories.
    Automatically relinks workspace URIs and project_id to the local machine.
    """
    if not SYNC_DIR.exists():
        return {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    stats = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

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

            # Skip ghost sessions (0 steps and no title/preview)
            step_cnt = meta.get("step_count", 0)
            t_val = (meta.get("title") or "").strip()
            p_val = (meta.get("preview") or "").strip()
            if (not step_cnt or step_cnt <= 0) and not t_val and not p_val:
                stats["skipped"] += 1
                continue

            # Only import if remote actually has conversation data (conversation.db or transcript)
            has_remote_conv = (entry / "conversation.db").exists()
            has_remote_transcript = (entry / "brain" / ".system_generated" / "logs" / "transcript.jsonl").exists()
            if not has_remote_conv and not has_remote_transcript:
                stats["skipped"] += 1
                continue

            # Standardize title and preview
            if not t_val and p_val:
                meta["title"] = p_val
            elif not p_val and t_val:
                meta["preview"] = t_val

            if project:
                ws_raw_chk = meta.get("workspace_uris", "")
                matched_p = False
                try:
                    uris = json.loads(ws_raw_chk)
                    for u in uris:
                        fld = Path(u.replace("file://", "")).name.lower()
                        if project.lower() == fld or project.lower() in u.lower():
                            matched_p = True
                            break
                except Exception:
                    if project.lower() in ws_raw_chk.lower():
                        matched_p = True
                if not matched_p:
                    continue

            remote_mtime = meta.get("last_modified_time")
            remote_dt = parse_sqlite_timestamp(remote_mtime)

            should_skip = True
            local_exists = False
            for db_path in [APP_CONV_DB, CLI_CONV_DB]:
                if not db_path.exists():
                    continue
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
                    cur = conn.cursor()
                    cur.execute("SELECT last_modified_time, raw_summary, project_id FROM conversation_summaries WHERE conversation_id = ?", (cid,))
                    row = cur.fetchone()
                    conn.close()
                    if not row:
                        should_skip = False
                        break
                    local_exists = True
                    local_dt = parse_sqlite_timestamp(row[0])
                    has_local_raw = row[1] is not None and len(row[1]) > 0
                    local_pid = row[2]
                    conv_db_local = find_local_conversation_db(cid)
                    has_remote_conv = (entry / "conversation.db").exists()
                    has_remote_raw = bool(meta.get("_raw_summary_b64"))

                    if not local_dt or str(local_dt).startswith("0001-01-01") or not local_pid:
                        should_skip = False
                        break
                    if remote_dt and local_dt < remote_dt:
                        should_skip = False
                        break
                    if has_remote_conv and not conv_db_local:
                        should_skip = False
                        break
                    if has_remote_raw and not has_local_raw:
                        should_skip = False
                        break
                except Exception:
                    should_skip = False
                    break

            if should_skip and not force:
                stats["skipped"] += 1
                continue

            # 1. Copy conversation.db to conversations/ folders safely
            conv_db_src = entry / "conversation.db"
            if conv_db_src.exists():
                for base in [GEMINI_HOME / "antigravity", GEMINI_HOME / "antigravity-cli"]:
                    if base.exists():
                        conv_dir = base / "conversations"
                        conv_dir.mkdir(parents=True, exist_ok=True)
                        dest_db = conv_dir / f"{cid}.db"

                        # SAFETY CHECK: Compare step counts before overwriting!
                        should_copy_db = True
                        if dest_db.exists():
                            # Safely checkpoint local WAL first
                            try:
                                chk = sqlite3.connect(f"file:{dest_db}?mode=rw", uri=True, timeout=3.0)
                                chk.execute("PRAGMA wal_checkpoint(FULL);")
                                chk.close()
                            except Exception:
                                pass

                            local_cnt = 0
                            remote_cnt = 0
                            try:
                                c_loc = sqlite3.connect(f"file:{dest_db}?mode=ro", uri=True, timeout=2.0)
                                local_cnt = c_loc.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
                                c_loc.close()
                            except Exception:
                                pass
                            try:
                                c_rem = sqlite3.connect(f"file:{conv_db_src}?mode=ro", uri=True, timeout=2.0)
                                remote_cnt = c_rem.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
                                c_rem.close()
                            except Exception:
                                pass

                            # If local has equal or more steps, NEVER overwrite it!
                            if local_cnt >= remote_cnt and local_cnt > 0:
                                should_copy_db = False

                        if should_copy_db:
                            shutil.copy2(conv_db_src, dest_db)
                            # NEVER delete .db-wal or .db-shm!

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
                if not target_proj or not target_proj.get("id"):
                    target_proj = ensure_local_project_for_workspace(local_folder)
                    projects_map[folder_name_lower] = target_proj

                meta["project_id"] = target_proj["id"]
                meta["workspace_uris"] = json.dumps([target_proj.get("folderUri") or local_folder.as_uri()])

                target_uri = target_proj.get("folderUri") or local_folder.as_uri()
                meta["project_id"] = target_proj["id"]
                meta["workspace_uris"] = json.dumps([target_uri])

                if raw_bytes:
                    raw_bytes = patch_protobuf_blob(
                        raw_bytes,
                        old_pid=old_pid,
                        new_pid=target_proj["id"],
                        new_uri=target_uri
                    )
                else:
                    try:
                        raw_bytes = synthesize_raw_summary(
                            cid=cid,
                            title=meta.get("title"),
                            preview=meta.get("preview"),
                            step_count=meta.get("step_count"),
                            mtime_str=meta.get("last_modified_time"),
                            ws_raw=json.dumps([target_uri]),
                            pid=target_proj["id"]
                        )
                    except Exception:
                        pass

            # If title is empty in meta, fall back to preview so Antigravity Desktop App won't show 'Untitled Conversation'
            if not (meta.get("title") or "").strip() and (meta.get("preview") or "").strip():
                meta["title"] = meta["preview"].strip()

            # Normalize running status to IDLE for imported sessions
            if meta.get("status") == "CASCADE_RUN_STATUS_RUNNING":
                meta["status"] = "IDLE"
            meta["not_fully_idle"] = 0

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

    # Run deduplication and relinking across all sessions
    deduplicate_and_relink_projects()

    return stats


def sync_all(limit: int = 0, force: bool = False, project: Optional[str] = None) -> Dict[str, Any]:
    """
    Full 2-way synchronization:
    1. Import newer/missing sessions from SYNC_DIR (coming from other devices via Syncthing).
    2. Export local sessions to SYNC_DIR (pushing updates to other devices).
    3. Deduplicate and relink sessions to local project paths.
    """
    import_stats = import_sessions(force=force, project=project)
    export_stats = export_sessions(limit=limit, force=force, project=project)
    relink_stats = deduplicate_and_relink_projects()

    return {
        "imported": import_stats["imported"],
        "updated": import_stats["updated"],
        "exported": export_stats["exported"],
        "skipped": export_stats["skipped"] + import_stats["skipped"],
        "relinked": relink_stats.get("relinked", 0),
        "duplicates_removed": relink_stats.get("duplicates_removed", 0),
        "errors": import_stats["errors"] + export_stats["errors"]
    }


def auto_import_synced_sessions():
    """
    Lightweight, silent auto-import of newly received Syncthing sessions.
    Called automatically when listing or resuming sessions.
    Also ensures agyhub_summaries_proto.pb is patched to include all DB sessions.
    """
    try:
        if not SYNC_DIR.exists():
            return
        import_sessions()
    except Exception:
        pass

    # Always patch agyhub_summaries_proto.pb to ensure App sidebar shows all sessions
    try:
        projects_map = get_local_projects_map()
        patch_agyhub_summaries_pb(projects_map)
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
