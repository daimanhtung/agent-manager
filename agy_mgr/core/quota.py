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
                m_csrf = re.search(r"--csrf_token\s+([a-zA-Z0-9-]+)", line)
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
    for port in ls_info.get("ports", []):
        url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
        try:
            req = urllib.request.Request(
                url,
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "X-Codeium-Csrf-Token": csrf
                }
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return parse_quota_response(data)
        except Exception:
            continue
    return None


def parse_quota_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Parse raw RetrieveUserQuotaSummaryResponse into a clean dictionary."""
    groups = raw.get("response", {}).get("groups", [])
    parsed = {
        "gemini_5h_fraction": None,
        "gemini_5h_reset": None,
        "gemini_weekly_fraction": None,
        "gemini_weekly_reset": None,
        "claude_weekly_fraction": None,
        "claude_weekly_reset": None,
        "raw_groups": groups,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

    for g in groups:
        dname = g.get("displayName", "").lower()
        buckets = g.get("buckets", [])
        if "gemini" in dname:
            for b in buckets:
                win = b.get("window", "")
                frac = b.get("remainingFraction")
                reset = b.get("resetTime")
                if win == "5h" or "five hour" in b.get("displayName", "").lower():
                    parsed["gemini_5h_fraction"] = frac
                    parsed["gemini_5h_reset"] = reset
                elif win == "weekly" or "weekly" in b.get("displayName", "").lower():
                    parsed["gemini_weekly_fraction"] = frac
                    parsed["gemini_weekly_reset"] = reset
        elif "claude" in dname or "gpt" in dname:
            for b in buckets:
                parsed["claude_weekly_fraction"] = b.get("remainingFraction")
                parsed["claude_weekly_reset"] = b.get("resetTime")

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


def fetch_quota_isolated_for_profile(profile_name: str) -> Optional[Dict[str, Any]]:
    """
    Query exact live quota for any profile independently in an isolated sandbox.
    Does not touch active user, macOS Keychain, or Desktop App.
    """
    from agy_mgr.config import PROFILES_DIR
    import socket
    import tempfile
    import shutil
    from pathlib import Path

    src_profile = PROFILES_DIR / profile_name
    if not src_profile.exists():
        return None

    temp_dir = Path(tempfile.mkdtemp(prefix=f"agy_quota_{profile_name}_"))
    try:
        temp_gemini = temp_dir / ".gemini"
        shutil.copytree(src_profile, temp_gemini)

        # Find an available local port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            free_port = s.getsockname()[1]

        csrf = f"csrf-{profile_name}-{int(time.time())}"
        env = os.environ.copy()
        env["HOME"] = str(temp_dir)

        cmd = [
            "/Applications/Antigravity.app/Contents/Resources/bin/language_server",
            "--standalone",
            "--csrf_token", csrf,
            "--cloud_code_endpoint", "https://daily-cloudcode-pa.googleapis.com",
            "--http_server_port", str(free_port),
            "--https_server_port", "0"
        ]

        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            url = f"http://127.0.0.1:{free_port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            req = urllib.request.Request(
                url,
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "X-Codeium-Csrf-Token": csrf
                }
            )
            for _ in range(20):
                time.sleep(0.2)
                try:
                    with urllib.request.urlopen(req, timeout=1.5) as resp:
                        data = json.loads(resp.read().decode())
                        return parse_quota_response(data)
                except Exception:
                    continue
        finally:
            proc.kill()
    except Exception:
        pass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return None


def get_all_accounts_quota(refresh_all: bool = False) -> List[Dict[str, Any]]:
    """Retrieve quota for all registered accounts, using isolated query per profile for true independent quota."""
    from agy_mgr.core.accounts import get_state, save_state

    accounts = list_accounts()
    active_name = get_active_account_name()
    state = get_state()
    cached_quotas = state.get("cached_quotas", {})

    # If refresh_all is requested, fetch live quota for each account independently
    if refresh_all:
        for acc in accounts:
            name = acc["name"]
            q = fetch_quota_isolated_for_profile(name)
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
            "gemini_5h": None,
            "gemini_weekly": None,
            "claude_weekly": None,
            "reset_5h": None,
            "reset_weekly": None,
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
        # For active account (when not refresh_all), or if account not yet in cache, fetch it
        if item["is_active"] and not refresh_all:
            q = fetch_quota_isolated_for_profile(name)
            if q:
                cached_quotas[name] = q
                state["cached_quotas"] = cached_quotas
                save_state(state)
        elif name in cached_quotas:
            q = cached_quotas[name]
        else:
            # First time seeing this account, fetch it
            q = fetch_quota_isolated_for_profile(name)
            if q:
                cached_quotas[name] = q
                state["cached_quotas"] = cached_quotas
                save_state(state)

        if q:
            item["gemini_5h"] = q.get("gemini_5h_fraction")
            item["gemini_weekly"] = q.get("gemini_weekly_fraction")
            item["claude_weekly"] = q.get("claude_weekly_fraction")
            item["reset_5h"] = format_reset_time(q.get("gemini_5h_reset"))
            item["reset_weekly"] = format_reset_time(q.get("gemini_weekly_reset"))
            if item["gemini_5h"] == 0 or item["gemini_weekly"] == 0:
                item["status"] = "Exhausted"
            elif item["is_active"]:
                item["status"] = "Active (Live)"
            else:
                item["status"] = "Standby (Cached)"
        else:
            item["status"] = "Standby (Available)"

        result.append(item)

    return result
