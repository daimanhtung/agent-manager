import os
import subprocess
from pathlib import Path
from typing import List, Optional

from agy_mgr.config import APP_PROFILES_DIR, ANTIGRAVITY_APP_PATH


def is_antigravity_running() -> bool:
    """Check if Antigravity.app is currently running on macOS."""
    try:
        out = subprocess.check_output(["pgrep", "-f", "Antigravity.app"], text=True)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def list_app_profiles() -> List[str]:
    """List available App instance profiles."""
    if not APP_PROFILES_DIR.exists():
        return []
    return [p.name for p in sorted(APP_PROFILES_DIR.iterdir()) if p.is_dir()]


def launch_app_instance(profile_name: str, workspace_path: Optional[str] = None) -> bool:
    """
    Launch a dedicated, isolated Antigravity Desktop App instance for the given profile.
    Uses 'open -n -a /Applications/Antigravity.app' with custom HOME and --user-data-dir.
    """
    if not ANTIGRAVITY_APP_PATH.exists():
        raise FileNotFoundError(f"Không tìm thấy Antigravity.app tại {ANTIGRAVITY_APP_PATH}")

    profile_home = APP_PROFILES_DIR / profile_name
    user_data_dir = profile_home / "app-data"
    profile_home.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "open", "-n", "-a", str(ANTIGRAVITY_APP_PATH),
        "--args",
        f"--user-data-dir={user_data_dir}"
    ]

    if workspace_path and Path(workspace_path).exists():
        cmd.append(str(Path(workspace_path).resolve()))

    env = os.environ.copy()
    env["HOME"] = str(profile_home)

    try:
        subprocess.Popen(cmd, env=env)
        return True
    except Exception as e:
        print(f"Lỗi khi mở Antigravity App: {e}")
        return False
