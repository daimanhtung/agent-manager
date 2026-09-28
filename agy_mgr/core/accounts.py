import json
import shutil
import base64
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any

from agy_mgr.config import (
    GEMINI_HOME,
    PROFILES_DIR,
    ACTIVE_FILE,
    STATE_FILE,
    AUTH_FILES,
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


def save_profile(name: str, gemini_dir: Path = GEMINI_HOME, metadata_extra: Optional[Dict] = None) -> Path:
    """Save credentials from gemini_dir into profiles/<name>."""
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

    email = (metadata_extra or {}).get("email") or existing_meta.get("email")
    if not email or email == "unknown@user":
        email = extract_email_from_gemini_dir(target_dir)

    meta = {
        "name": name,
        "email": email,
        "saved_at": datetime.now().isoformat(),
        "type": "oauth"
    }
    if existing_meta:
        meta.update({k: v for k, v in existing_meta.items() if k not in ["saved_at"]})
    if metadata_extra:
        meta.update(metadata_extra)

    with open(target_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return target_dir


def load_profile(name: str, gemini_dir: Path = GEMINI_HOME) -> bool:
    """Restore credentials from profiles/<name> into gemini_dir."""
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

    # Update google_accounts.json
    email = extract_email_from_gemini_dir(gemini_dir)
    if email and email != "unknown@user":
        gacc = gemini_dir / "google_accounts.json"
        try:
            with open(gacc, "w", encoding="utf-8") as f:
                json.dump({"active": email, "old": []}, f, indent=2)
        except Exception:
            pass

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


def switch_account(target_name: str) -> bool:
    """
    Switch active account:
    1. Backup current active account so freshly refreshed tokens are not lost
    2. Load target account credentials into ~/.gemini
    3. Restart / signal Antigravity language_server if running
    """
    current_active = get_active_account_name()
    if current_active and (PROFILES_DIR / current_active).exists():
        save_profile(current_active)

    if not load_profile(target_name):
        return False

    # Notify / Restart running language_server if present
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
            if "language_server" in line and "--standalone" in line:
                pid = line.split()[1]
                # Sending SIGHUP or SIGTERM allows Electron host to automatically respawn it with new credentials
                import signal
                os.kill(int(pid), signal.SIGTERM)
                break
    except Exception:
        pass
