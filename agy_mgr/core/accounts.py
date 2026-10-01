import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agy_mgr.config import (
    GEMINI_HOME,
    PROFILES_DIR,
    ACTIVE_FILE,
    STATE_FILE,
    AUTH_FILES,
    ANTIGRAVITY_APP_PATH,
    ANTIGRAVITY_APP_SUPPORT_DIR,
    ANTIGRAVITY_VSCDB,
    ANTIGRAVITY_APP_STORAGE,
)


def decode_jwt_email(jwt_token: str) -> Optional[str]:
    """Decode email from JWT id_token without verifying signature."""
    try:
        parts = jwt_token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            padded = payload + "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded))
            return data.get("email")
    except Exception:
        pass
    return None


def decode_jwt_claims(jwt_token: str) -> Dict[str, Any]:
    """Decode all claims (email, name, picture) from JWT id_token without verifying signature."""
    try:
        parts = jwt_token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            padded = payload + "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        pass
    return {}


def extract_email_from_gemini_dir(gemini_dir: Path = GEMINI_HOME) -> str:
    """Extract email from tokens in a gemini directory, prioritizing the freshest token file."""
    candidates = [
        gemini_dir / "antigravity-cli" / "antigravity-oauth-token",
        gemini_dir / "jetski-standalone-oauth-token",
        gemini_dir / "oauth_creds.json",
    ]
    # Sort by mtime descending (most recently modified first)
    existing = [p for p in candidates if p.exists()]
    existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for p in existing:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
                id_token = d.get("id_token") or (d.get("token", {}).get("id_token") if isinstance(d.get("token"), dict) else None)
                if id_token:
                    em = decode_jwt_email(id_token)
                    if em:
                        return em
        except Exception:
            pass

    # Fallback: Try google_accounts.json
    p_gacc = gemini_dir / "google_accounts.json"
    if p_gacc.exists():
        try:
            with open(p_gacc, "r", encoding="utf-8") as f:
                d = json.load(f)
                if d.get("active"):
                    return d["active"]
        except Exception:
            pass

    return "unknown@user"


def get_active_account_name() -> Optional[str]:
    """Return active profile name from active.json."""
    if ACTIVE_FILE.exists():
        try:
            with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("active")
        except Exception:
            pass
    return None


def set_active_account_name(name: str):
    """Set active profile name."""
    with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
        json.dump({"active": name, "updated_at": datetime.now().isoformat()}, f, indent=2)


def get_state() -> Dict[str, Any]:
    """Load persistent state (e.g. cooldowns, stats)."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"cooldowns": {}, "stats": {}}


def save_state(state: Dict[str, Any]):
    """Save persistent state."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def set_account_cooldown(name: str, duration_seconds: int = 1800, reason: str = "Rate limit / Quota reached"):
    """Mark account in cooldown until now + duration_seconds."""
    state = get_state()
    cooldowns = state.get("cooldowns", {})
    cooldowns[name] = {
        "until": time.time() + duration_seconds,
        "reason": reason,
        "set_at": datetime.now().isoformat()
    }
    state["cooldowns"] = cooldowns
    save_state(state)


def get_account_cooldown(name: str) -> Optional[Dict[str, Any]]:
    """Return cooldown info if account is currently in cooldown."""
    state = get_state()
    cd = state.get("cooldowns", {}).get(name)
    if cd:
        if time.time() < cd.get("until", 0):
            return cd
        # Expired cooldown, remove it
        del state["cooldowns"][name]
        save_state(state)
    return None


def get_from_macos_keychain() -> Optional[Dict[str, Any]]:
    """Read token dict from macOS Keychain (service: gemini, account: antigravity)."""
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        if raw.startswith("go-keyring-base64:"):
            b64 = raw.split("go-keyring-base64:")[1]
            return json.loads(base64.b64decode(b64).decode())
    except Exception:
        pass
    return None


def sync_to_macos_keychain(token_dict: Dict[str, Any]) -> bool:
    """Update macOS Keychain (service: gemini, account: antigravity) with given token dict."""
    try:
        raw_json = json.dumps(token_dict)
        b64 = base64.b64encode(raw_json.encode()).decode()
        payload = f"go-keyring-base64:{b64}"
        subprocess.run([
            "security", "add-generic-password", "-U",
            "-s", "gemini",
            "-a", "antigravity",
            "-w", payload
        ], capture_output=True, check=True)
        return True
    except Exception:
        return False


def delete_from_macos_keychain() -> bool:
    """Delete entry from macOS Keychain."""
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", "gemini", "-a", "antigravity"],
            capture_output=True, check=True
        )
        return True
    except Exception:
        return False


def _encode_varint(val: int) -> bytes:
    res = bytearray()
    while val > 0x7f:
        res.append((val & 0x7f) | 0x80)
        val >>= 7
    res.append(val & 0x7f)
    return bytes(res)


def _encode_field(tag: int, wire: int, data: bytes) -> bytes:
    header = _encode_varint((tag << 3) | wire)
    if wire == 2:
        return header + _encode_varint(len(data)) + data
    elif wire == 0:
        return header + data
    return header + data


def _get_template_user_status_pb() -> bytes:
    """Return raw template protobuf bytes for userStatus."""
    # 1. Try from existing state.vscdb
    if ANTIGRAVITY_VSCDB.exists():
        try:
            conn = sqlite3.connect(ANTIGRAVITY_VSCDB, timeout=2.0)
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key = 'antigravityAuthStatus'")
            row = cur.fetchone()
            conn.close()
            if row:
                d = json.loads(row[0])
                b64_str = d.get("userStatusProtoBinaryBase64")
                if b64_str:
                    return base64.b64decode(b64_str + "===")
        except Exception:
            pass

    # 2. Try _template.b64 file
    tpl_file = Path(__file__).parent / "_template.b64"
    if tpl_file.exists():
        try:
            return base64.b64decode(tpl_file.read_text().strip() + "===")
        except Exception:
            pass

    return b""


def _synthesize_user_status_proto(template_pb: bytes, name: str, email: str) -> bytes:
    """Replace tag 3 (name) and tag 7 (email) in userStatus protobuf while keeping model capabilities intact."""
    if not template_pb:
        # Fallback minimal proto
        f_name = _encode_field(3, 2, name.encode("utf-8"))
        f_email = _encode_field(7, 2, email.encode("utf-8"))
        return f_name + f_email

    i = 0
    remaining = None
    while i < len(template_pb):
        byte = template_pb[i]
        i += 1
        tag = byte >> 3
        wire = byte & 7
        if wire == 0:
            while template_pb[i] & 0x80:
                i += 1
            i += 1
        elif wire == 2:
            length = 0
            shift = 0
            while True:
                vb = template_pb[i]
                i += 1
                length |= (vb & 0x7f) << shift
                if not (vb & 0x80):
                    break
                shift += 7
            chunk = template_pb[i:i + length]
            i += length
            if tag == 7:  # tag 7 is email; remainder has capabilities and tiers
                remaining = template_pb[i:]
                break

    f_name = _encode_field(3, 2, name.encode("utf-8"))
    f_email = _encode_field(7, 2, email.encode("utf-8"))
    return f_name + f_email + (remaining if remaining is not None else b"")


def _build_user_status_val(new_raw_pb: bytes) -> str:
    """Build base64 outer protobuf for antigravityUnifiedStateSync.userStatus."""
    new_b64 = base64.b64encode(new_raw_pb).decode("utf-8")
    sub = _encode_field(1, 2, b"userStatusSentinelKey") + _encode_field(2, 2, _encode_field(1, 2, new_b64.encode("utf-8")))
    outer = _encode_field(1, 2, sub)
    return base64.b64encode(outer).decode("utf-8")


def _build_inner_token_pb(access_token: str, refresh_token: str, exp_val: int = 0) -> bytes:
    """Build inner token protobuf (tag 1: access_token, tag 2: Bearer, tag 3: refresh_token, tag 4: expiry)."""
    f1 = _encode_field(1, 2, access_token.encode("utf-8"))
    f2 = _encode_field(2, 2, b"Bearer")
    f3 = _encode_field(3, 2, refresh_token.encode("utf-8"))
    if exp_val == 0:
        exp_val = int(time.time() * 1000) + 3600 * 1000
    exp_inner = _encode_varint((1 << 3) | 0) + _encode_varint(exp_val)
    f4 = _encode_field(4, 2, exp_inner)
    return f1 + f2 + f3 + f4


def _build_oauth_token_val(inner_pb: bytes) -> str:
    """Build base64 outer protobuf for antigravityUnifiedStateSync.oauthToken."""
    inner_b64 = base64.b64encode(inner_pb).decode("utf-8")
    s1_inner = _encode_field(1, 2, inner_b64.encode("utf-8"))
    s1 = _encode_field(1, 2, b"oauthTokenInfoSentinelKey") + _encode_field(2, 2, s1_inner)
    ctx_json = '{"state":"signedIn","context":{"project":"","showProjectError":false,"errorMessage":"","ineligibleMessage":"","verificationUrl":"","isGcpTos":false,"browserOpenFailed":false,"appealUrl":"","appealLinkText":""}}'
    s2_inner = _encode_field(1, 2, ctx_json.encode("utf-8"))
    s2 = _encode_field(1, 2, b"authStateWithContextSentinelKey") + _encode_field(2, 2, s2_inner)
    outer = _encode_field(1, 2, s1) + _encode_field(1, 2, s2)
    return base64.b64encode(outer).decode("utf-8")


def is_antigravity_desktop_running() -> bool:
    """Check if Antigravity Desktop App GUI process is running."""
    try:
        out = subprocess.check_output(["pgrep", "-f", "Antigravity.app/Contents/MacOS/Antigravity"], text=True)
        return bool(out.strip())
    except Exception:
        try:
            out = subprocess.check_output(["pgrep", "-f", "Antigravity Helper (Renderer)"], text=True)
            return bool(out.strip())
        except Exception:
            return False


def restart_antigravity_desktop(timeout_seconds: int = 5) -> bool:
    """
    Gracefully restart Antigravity Desktop App so it loads the new account credentials.
    All open workspaces, tabs, and dirty files are preserved by Electron/VS Code session restore.
    """
    if not is_antigravity_desktop_running():
        return False

    print("[*] Đang khởi động lại Antigravity Desktop App để chuyển tài khoản...")
    try:
        subprocess.run(["osascript", "-e", 'tell application "Antigravity" to quit'], capture_output=True, timeout=3)
    except Exception:
        pass

    t0 = time.time()
    while time.time() - t0 < timeout_seconds:
        if not is_antigravity_desktop_running():
            break
        time.sleep(0.5)

    if is_antigravity_desktop_running():
        try:
            subprocess.run(["pkill", "-f", "Antigravity.app/Contents/MacOS/Antigravity"], capture_output=True)
            time.sleep(0.5)
        except Exception:
            pass

    # Terminate old language_server instances
    refresh_antigravity_app()

    try:
        subprocess.Popen(["open", "-a", str(ANTIGRAVITY_APP_PATH)])
        print("[✓] Đã khởi động lại Antigravity Desktop App thành công!")
        return True
    except Exception as e:
        print(f"[!] Không thể tự động mở lại Antigravity Desktop App: {e}")
        return False


def save_vscdb_auth_for_profile(name: str):
    """Backup current Antigravity Desktop App state.vscdb entries for profile if email matches."""
    if not ANTIGRAVITY_VSCDB.exists():
        return

    profile_dir = PROFILES_DIR / name
    if not profile_dir.exists():
        return

    meta_file = profile_dir / "meta.json"
    expected_email = None
    if meta_file.exists():
        try:
            expected_email = json.loads(meta_file.read_text()).get("email")
        except Exception:
            pass

    try:
        conn = sqlite3.connect(ANTIGRAVITY_VSCDB, timeout=3.0)
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout = 3000")
        cur.execute("SELECT value FROM ItemTable WHERE key = 'antigravityAuthStatus'")
        row = cur.fetchone()
        if not row:
            conn.close()
            return

        auth_data = json.loads(row[0])
        current_vscdb_email = auth_data.get("email")

        # Only save if the email matches this profile
        if expected_email and current_vscdb_email and expected_email != current_vscdb_email:
            conn.close()
            return

        keys = [
            "antigravityAuthStatus",
            "antigravityUnifiedStateSync.oauthToken",
            "antigravityUnifiedStateSync.userStatus",
            "antigravity.profileUrl",
            "jetskiStateSync.agentManagerInitState",
        ]
        saved_data = {}
        for k in keys:
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", (k,))
            r = cur.fetchone()
            if r:
                saved_data[k] = r[0]
        conn.close()

        if saved_data:
            with open(profile_dir / "vscdb_auth.json", "w", encoding="utf-8") as f:
                json.dump(saved_data, f, indent=2)
    except Exception:
        pass


def apply_vscdb_auth_for_profile(name: str):
    """
    Restore or synthesize Antigravity Desktop App state.vscdb credentials for profile.
    This guarantees Antigravity Desktop App loads the correct account on startup / reload!
    """
    if not ANTIGRAVITY_VSCDB.exists():
        return

    profile_dir = PROFILES_DIR / name
    if not profile_dir.exists():
        return

    vscdb_auth_file = profile_dir / "vscdb_auth.json"
    vscdb_data = {}

    if vscdb_auth_file.exists():
        try:
            vscdb_data = json.loads(vscdb_auth_file.read_text())
        except Exception:
            pass

    # If vscdb_data is missing or incomplete, synthesize it from tokens
    if not vscdb_data or "antigravityAuthStatus" not in vscdb_data:
        tok_file = profile_dir / "jetski-standalone-oauth-token"
        if not tok_file.exists():
            tok_file = profile_dir / "antigravity-cli" / "antigravity-oauth-token"
        if not tok_file.exists():
            tok_file = profile_dir / "keychain_token.json"

        if tok_file.exists():
            try:
                tok_data = json.loads(tok_file.read_text())
                idt = tok_data.get("id_token") or (tok_data.get("token", {}).get("id_token") if isinstance(tok_data.get("token"), dict) else None)
                claims = decode_jwt_claims(idt) if idt else {}
                email = claims.get("email") or name
                user_name = claims.get("name") or email.split("@")[0]
                picture = claims.get("picture", "")

                access_tok = (tok_data.get("token", {}).get("access_token") if isinstance(tok_data.get("token"), dict) else None) or tok_data.get("access_token", "")
                refresh_tok = (tok_data.get("token", {}).get("refresh_token") if isinstance(tok_data.get("token"), dict) else None) or tok_data.get("refresh_token", "")

                tpl_pb = _get_template_user_status_pb()
                new_status_pb = _synthesize_user_status_proto(tpl_pb, user_name, email)
                user_status_val = _build_user_status_val(new_status_pb)

                inner_tok_pb = _build_inner_token_pb(access_tok, refresh_tok)
                oauth_tok_val = _build_oauth_token_val(inner_tok_pb)

                auth_status = json.dumps({
                    "name": user_name,
                    "apiKey": access_tok,
                    "email": email,
                    "userStatusProtoBinaryBase64": base64.b64encode(new_status_pb).decode("utf-8")
                })

                vscdb_data = {
                    "antigravityAuthStatus": auth_status,
                    "antigravityUnifiedStateSync.oauthToken": oauth_tok_val,
                    "antigravityUnifiedStateSync.userStatus": user_status_val,
                    "antigravity.profileUrl": picture
                }
                # Save synthesized file for future instant reloads
                with open(vscdb_auth_file, "w", encoding="utf-8") as f:
                    json.dump(vscdb_data, f, indent=2)
            except Exception:
                pass

    if vscdb_data:
        try:
            conn = sqlite3.connect(ANTIGRAVITY_VSCDB, timeout=5.0)
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout = 5000")
            for k, v in vscdb_data.items():
                cur.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (k, v))
            # Delete cached quota metrics so the UI widgets immediately refresh for the new user
            cur.execute("DELETE FROM ItemTable WHERE key = 'n2ns.antigravity-panel'")
            conn.commit()
            conn.close()
        except Exception:
            pass


def save_profile(name: str, gemini_dir: Path = GEMINI_HOME, metadata_extra: Optional[Dict] = None) -> Path:
    """Save credentials from gemini_dir and state.vscdb into profiles/<name>."""
    target_dir = PROFILES_DIR / name
    target_dir.mkdir(parents=True, exist_ok=True)

    for rel_path in AUTH_FILES:
        src = gemini_dir / rel_path
        if src.exists():
            dst = target_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # Sync between jetski-standalone-oauth-token and antigravity-cli/antigravity-oauth-token if one exists
    f_standalone = target_dir / "jetski-standalone-oauth-token"
    f_cli = target_dir / "antigravity-cli" / "antigravity-oauth-token"
    if f_cli.exists() and not f_standalone.exists():
        shutil.copy2(f_cli, f_standalone)
    elif f_standalone.exists() and not f_cli.exists():
        f_cli.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f_standalone, f_cli)

    meta_file = target_dir / "meta.json"
    existing_meta = {}
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                existing_meta = json.load(f)
        except Exception:
            pass

    expected_email = (metadata_extra or {}).get("email") or existing_meta.get("email")

    # Backup macOS Keychain token ONLY if it matches this profile to prevent token contamination
    kc_tok = get_from_macos_keychain()
    if kc_tok:
        kc_email = None
        if kc_tok.get("id_token"):
            kc_email = decode_jwt_email(kc_tok["id_token"])

        if not expected_email or expected_email == "unknown@user" or kc_email == expected_email:
            with open(target_dir / "keychain_token.json", "w", encoding="utf-8") as f:
                json.dump(kc_tok, f, indent=2)
            with open(f_standalone, "w", encoding="utf-8") as f:
                json.dump(kc_tok, f, indent=2)
            f_cli.parent.mkdir(parents=True, exist_ok=True)
            with open(f_cli, "w", encoding="utf-8") as f:
                json.dump(kc_tok, f, indent=2)

    # Backup state.vscdb entries for this profile
    save_vscdb_auth_for_profile(name)

    # Extract real email directly from token
    real_email = None
    if kc_tok and kc_tok.get("id_token") and (not expected_email or decode_jwt_email(kc_tok["id_token"]) == expected_email):
        real_email = decode_jwt_email(kc_tok["id_token"])
    if not real_email or real_email == "unknown@user":
        real_email = extract_email_from_gemini_dir(target_dir)

    email = (metadata_extra or {}).get("email") or real_email or existing_meta.get("email") or "unknown@user"

    meta = {
        "name": name,
        "email": email,
        "saved_at": datetime.now().isoformat(),
        "type": "oauth"
    }
    if existing_meta:
        for k, v in existing_meta.items():
            if k not in ["saved_at", "email"]:
                meta[k] = v
    if metadata_extra:
        meta.update(metadata_extra)

    with open(target_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return target_dir


def load_profile(name: str, gemini_dir: Path = GEMINI_HOME) -> bool:
    """Restore credentials from profiles/<name> into gemini_dir, macOS Keychain, and Antigravity Desktop App state.vscdb."""
    src_dir = PROFILES_DIR / name
    if not src_dir.exists():
        return False

    for rel_path in AUTH_FILES:
        src = src_dir / rel_path
        if src.exists():
            dst = gemini_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # Ensure both token files exist in target gemini_dir
    f_standalone = gemini_dir / "jetski-standalone-oauth-token"
    f_cli = gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
    if f_cli.exists() and not f_standalone.exists():
        shutil.copy2(f_cli, f_standalone)
    elif f_standalone.exists() and not f_cli.exists():
        f_cli.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f_standalone, f_cli)

    # Synchronize to macOS Keychain so agy permanently respects this account!
    kc_file = src_dir / "keychain_token.json"
    token_to_sync = None
    if kc_file.exists():
        try:
            with open(kc_file, "r", encoding="utf-8") as f:
                token_to_sync = json.load(f)
        except Exception:
            pass

    if not token_to_sync:
        for p in [f_standalone, f_cli]:
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        token_to_sync = json.load(f)
                        if token_to_sync:
                            break
                except Exception:
                    pass

    if token_to_sync:
        sync_to_macos_keychain(token_to_sync)

    # Update google_accounts.json
    email = extract_email_from_gemini_dir(gemini_dir)
    if email and email != "unknown@user":
        gacc = gemini_dir / "google_accounts.json"
        try:
            with open(gacc, "w", encoding="utf-8") as f:
                json.dump({"active": email, "old": []}, f, indent=2)
        except Exception:
            pass

        # Update Antigravity Desktop App app_storage.json
        if ANTIGRAVITY_APP_STORAGE.exists():
            try:
                with open(ANTIGRAVITY_APP_STORAGE, "r", encoding="utf-8") as f:
                    app_data = json.load(f)
                app_data["jetski.onboarding.lastLoginUsername"] = email
                with open(ANTIGRAVITY_APP_STORAGE, "w", encoding="utf-8") as f:
                    json.dump(app_data, f, indent=2)
            except Exception:
                pass

    # Synchronize Antigravity Desktop App state.vscdb
    apply_vscdb_auth_for_profile(name)

    set_active_account_name(name)
    return True


def list_accounts() -> List[Dict[str, Any]]:
    """List all accounts with their status, email, active state, and cooldowns."""
    auto_import_current_if_empty()
    active_name = get_active_account_name()
    accounts = []

    for profile_path in sorted(PROFILES_DIR.iterdir()):
        if profile_path.is_dir():
            name = profile_path.name
            meta_file = profile_path / "meta.json"
            meta = {}
            if meta_file.exists():
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass

            email = meta.get("email") or extract_email_from_gemini_dir(profile_path)
            cd = get_account_cooldown(name)

            accounts.append({
                "name": name,
                "email": email,
                "type": meta.get("type", "oauth"),
                "is_active": (name == active_name),
                "cooldown": cd,
                "saved_at": meta.get("saved_at", ""),
                "path": str(profile_path)
            })

    return accounts


def switch_account(target_name: str, restart_app: bool = True) -> bool:
    """
    Switch active account:
    1. Backup current active account so freshly refreshed tokens are not lost
    2. Load target account credentials into ~/.gemini, macOS Keychain, and state.vscdb
    3. Restart Antigravity Desktop App (if running and restart_app is True)
    """
    current_active = get_active_account_name()
    if current_active and (PROFILES_DIR / current_active).exists():
        current_email = extract_email_from_gemini_dir(GEMINI_HOME)
        profile_meta_file = PROFILES_DIR / current_active / "meta.json"
        profile_email = None
        if profile_meta_file.exists():
            try:
                profile_email = json.loads(profile_meta_file.read_text()).get("email")
            except Exception:
                pass

        if current_email and profile_email and current_email != profile_email and current_email != "unknown@user":
            matching_prof = None
            for p in PROFILES_DIR.iterdir():
                if p.is_dir() and (p / "meta.json").exists():
                    try:
                        d = json.loads((p / "meta.json").read_text())
                        if d.get("email") == current_email:
                            matching_prof = p.name
                            break
                    except Exception:
                        pass
            if matching_prof:
                save_profile(matching_prof, gemini_dir=GEMINI_HOME)
        else:
            save_profile(current_active, gemini_dir=GEMINI_HOME)

    if not load_profile(target_name):
        return False

    # Handle running Antigravity Desktop App
    app_running = is_antigravity_desktop_running()
    if app_running:
        if restart_app:
            restart_antigravity_desktop()
        else:
            print("[!] Antigravity Desktop App đang chạy. Vui lòng bấm Cmd+Shift+P -> 'Developer: Reload Window' hoặc khởi động lại Antigravity để nạp tài khoản mới.")
            refresh_antigravity_app()
    else:
        refresh_antigravity_app()

    return True


def remove_account(name: str) -> bool:
    """Remove a profile from ~/.agy-manager/profiles."""
    profile_dir = PROFILES_DIR / name
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
        if get_active_account_name() == name:
            with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
                json.dump({"active": None}, f)
        return True
    return False


def auto_import_current_if_empty():
    """If no profiles exist yet, import current credentials in ~/.gemini as default."""
    existing = [p for p in PROFILES_DIR.iterdir() if p.is_dir()]
    if not existing:
        current_email = extract_email_from_gemini_dir(GEMINI_HOME)
        profile_name = current_email.split("@")[0] if "@" in current_email else "default"
        save_profile(profile_name)
        set_active_account_name(profile_name)


def refresh_antigravity_app():
    """
    Notify or restart background language_server so Antigravity App
    instantly updates to the newly switched account credentials.
    """
    import subprocess
    try:
        out = subprocess.check_output(["ps", "aux"], text=True)
        for line in out.splitlines():
            if "language_server" in line:
                parts = line.split()
                if len(parts) > 1:
                    pid = parts[1]
                    import signal
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except Exception:
                        pass
    except Exception:
        pass
