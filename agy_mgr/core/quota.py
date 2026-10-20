import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from agy_mgr.core.accounts import list_accounts, get_active_account_name


def find_running_language_server() -> Optional[Dict[str, Any]]:
    """Discover running Antigravity language_server process, csrf_token, and listen port."""
    try:
        out = subprocess.check_output(["ps", "aux"], text=True)
        for line in out.splitlines():
            if "language_server" in line and "--csrf_token" in line:
                # Ignore isolated temporary quota sandbox processes
                if "agy_quota_" in line or "agy_test_" in line or "agy_debug_" in line:
                    continue
                m_csrf = re.search(r"--csrf_token[=\s]+([a-zA-Z0-9-]+)", line)
                parts = line.split()
                if len(parts) > 1 and m_csrf:
                    pid = parts[1]
                    csrf = m_csrf.group(1)
                    # Find open ports
                    lsof = subprocess.check_output(["lsof", "-nP", "-p", pid], text=True)
                    ports = []
                    for l in lsof.splitlines():
                        if "LISTEN" in l:
                            m_port = re.search(r":(\d+)\s+\(LISTEN\)", l)
                            if m_port:
                                ports.append(int(m_port.group(1)))
                    if ports:
                        return {"pid": pid, "csrf": csrf, "ports": ports}
    except Exception:
        pass
    return None


def fetch_quota_from_ls(ls_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Query live quota from Antigravity language_server."""
    csrf = ls_info.get("csrf")
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx)
    )

    for port in ls_info.get("ports", []):
        for proto in ["https", "http"]:
            url = f"{proto}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            try:
                req = urllib.request.Request(
                    url,
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Connect-Protocol-Version": "1",
                        "X-Codeium-Csrf-Token": csrf
                    }
                )
                with opener.open(req, timeout=1.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    parsed = parse_quota_response(data)
                    if parsed and (parsed.get("gemini_5h_fraction") is not None or parsed.get("claude_weekly_fraction") is not None):
                        return parsed
            except Exception:
                continue
    return None


def parse_quota_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Parse raw RetrieveUserQuotaSummaryResponse into a clean dictionary."""
    groups = raw.get("response", {}).get("groups", [])
    if not groups and "groups" in raw:
        groups = raw.get("groups", [])

    parsed = {
        "gemini_5h_fraction": None,
        "gemini_5h_reset": None,
        "gemini_weekly_fraction": None,
        "gemini_weekly_reset": None,
        "claude_5h_fraction": None,
        "claude_5h_reset": None,
        "claude_weekly_fraction": None,
        "claude_weekly_reset": None,
        "gemini_unlimited": True,
        "raw_groups": groups,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

    for g in groups:
        dname = g.get("displayName", "").lower()
        buckets = g.get("buckets", [])
        if "gemini" in dname:
            parsed["gemini_unlimited"] = False
            for b in buckets:
                win = b.get("window", "").lower()
                bid = b.get("bucketId", "").lower()
                bname = b.get("displayName", "").lower()
                frac = b.get("remainingFraction")
                reset = b.get("resetTime")
                if win == "5h" or "5h" in bid or "five hour" in bname:
                    parsed["gemini_5h_fraction"] = frac
                    parsed["gemini_5h_reset"] = reset
                elif win == "weekly" or "weekly" in bid or "weekly" in bname:
                    parsed["gemini_weekly_fraction"] = frac
                    parsed["gemini_weekly_reset"] = reset
        elif "claude" in dname or "gpt" in dname or "3p" in dname:
            for b in buckets:
                win = b.get("window", "").lower()
                bid = b.get("bucketId", "").lower()
                bname = b.get("displayName", "").lower()
                frac = b.get("remainingFraction")
                reset = b.get("resetTime")
                if win == "5h" or "5h" in bid or "five hour" in bname:
                    parsed["claude_5h_fraction"] = frac
                    parsed["claude_5h_reset"] = reset
                elif win == "weekly" or "weekly" in bid or "weekly" in bname:
                    parsed["claude_weekly_fraction"] = frac
                    parsed["claude_weekly_reset"] = reset

    return parsed


def format_reset_time(iso_str: Optional[str]) -> str:
    """Format ISO timestamp into human friendly countdown (e.g. '2h 15m' or '2d 4h')."""
    if not iso_str:
        return "N/A"
    try:
        # replace Z with +00:00 for fromisoformat
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = dt - now
        secs = int(diff.total_seconds())
        if secs <= 0:
            return "Sẵn sàng (Reset xong)"
        days = secs // 86400
        hours = (secs % 86400) // 3600
        mins = (secs % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m {secs % 60}s"
    except Exception:
        return iso_str


def fetch_quota_direct_http(profile_name: str) -> Optional[Dict[str, Any]]:
    """
    Query live quota directly from Google Cloud Code PA API using profile access token.
    Never spawns language_server, never alters $HOME, never accesses macOS Keychain.
    Completely eliminates any macOS Keychain reset prompts or delays.
    """
    from agy_mgr.config import PROFILES_DIR
    profile_dir = PROFILES_DIR / profile_name
    if not profile_dir.exists():
        return None

    # Find valid access_token from saved profile files
    access_token = None
    for token_file_name in ["jetski-standalone-oauth-token", "keychain_token.json", "antigravity-cli/antigravity-oauth-token"]:
        f = profile_dir / token_file_name
        if f.exists():
            try:
                d = json.loads(f.read_text())
                tok = d.get("token")
                if isinstance(tok, dict) and tok.get("access_token"):
                    access_token = tok["access_token"]
                    break
                elif isinstance(tok, str) and tok.startswith("ya29."):
                    access_token = tok
                    break
                elif d.get("access_token"):
                    access_token = d["access_token"]
                    break
            except Exception:
                pass

    if not access_token:
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/2.24.0"
    }

    endpoints = [
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
        "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
    ]

    for ep in endpoints:
        try:
            req = urllib.request.Request(ep, data=b"{}", headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                parsed = parse_quota_response(data)
                if parsed and (parsed.get("gemini_5h_fraction") is not None or parsed.get("gemini_weekly_fraction") is not None):
                    return parsed
        except Exception:
            continue

    return None


def get_all_accounts_quota(refresh_all: bool = False) -> List[Dict[str, Any]]:
    """Retrieve quota for all registered accounts using running language_server for active and direct HTTP / cache for standby."""
    from agy_mgr.core.accounts import get_state, save_state

    accounts = list_accounts()
    active_name = get_active_account_name()
    state = get_state()
    cached_quotas = state.get("cached_quotas", {})

    # If refresh_all is requested, fetch live quota for each account via direct HTTP API
    if refresh_all:
        for acc in accounts:
            name = acc["name"]
            q = None
            if name == active_name:
                ls_info = find_running_language_server()
                if ls_info:
                    q = fetch_quota_from_ls(ls_info)
            if not q:
                q = fetch_quota_direct_http(name)
            if q:
                cached_quotas[name] = q
        state["cached_quotas"] = cached_quotas
        save_state(state)

    result = []
    for acc in accounts:
        name = acc["name"]
        item = {
            "name": name,
            "email": acc["email"],
            "is_active": (name == active_name),
            "cooldown": acc.get("cooldown"),
            "claude_5h": None,
            "claude_weekly": None,
            "reset_5h": None,
            "reset_weekly": None,
            "gemini_5h": None,
            "gemini_weekly": None,
            "gemini_unlimited": True,
            "status": "Ready"
        }

        # Check cooldown first
        if acc.get("cooldown"):
            cd = acc["cooldown"]
            until = cd.get("until", 0)
            remaining_mins = max(0, int((until - time.time()) / 60))
            item["status"] = f"Cooldown ({remaining_mins}m)"
            result.append(item)
            continue

        q = None
        # For active account (when not refresh_all), query running language_server or fallback to direct HTTP
        if item["is_active"] and not refresh_all:
            ls_info = find_running_language_server()
            if ls_info:
                q = fetch_quota_from_ls(ls_info)
            if not q:
                q = fetch_quota_direct_http(name)
            if q:
                cached_quotas[name] = q
                state["cached_quotas"] = cached_quotas
                save_state(state)
        elif name in cached_quotas:
            q = cached_quotas[name]
        else:
            # First time seeing this account, try direct HTTP query
            q = fetch_quota_direct_http(name)
            if q:
                cached_quotas[name] = q
                state["cached_quotas"] = cached_quotas
                save_state(state)

        # Upgrade existing cached quota from raw_groups if missing new fields
        if q and ("claude_5h_fraction" not in q or "gemini_unlimited" not in q):
            raw_g = q.get("raw_groups", [])
            if raw_g:
                reparsed = parse_quota_response({"groups": raw_g})
                reparsed["updated_at"] = q.get("updated_at", reparsed["updated_at"])
                q = reparsed
                cached_quotas[name] = q
                state["cached_quotas"] = cached_quotas
                save_state(state)

        if q:
            item["gemini_5h"] = q.get("gemini_5h_fraction")
            item["gemini_weekly"] = q.get("gemini_weekly_fraction")
            item["claude_5h"] = q.get("claude_5h_fraction")
            item["claude_weekly"] = q.get("claude_weekly_fraction")
            item["reset_5h"] = format_reset_time(q.get("gemini_5h_reset") or q.get("claude_5h_reset"))
            item["reset_weekly"] = format_reset_time(q.get("gemini_weekly_reset") or q.get("claude_weekly_reset"))
            item["claude_reset"] = format_reset_time(q.get("claude_weekly_reset"))

            # Quota exhausted detection
            is_exhausted = (
                (item["gemini_5h"] is not None and item["gemini_5h"] == 0) or
                (item["gemini_weekly"] is not None and item["gemini_weekly"] == 0) or
                (item["claude_weekly"] is not None and item["claude_weekly"] == 0)
            )
            if is_exhausted:
                item["status"] = "Exhausted"
            elif item["is_active"]:
                item["status"] = "Active (Live)"
            elif refresh_all:
                item["status"] = "Standby (Live)"
            else:
                item["status"] = "Standby (Cached)"
        else:
            item["status"] = "Standby (Available)"

        result.append(item)

    return result
