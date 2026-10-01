from pathlib import Path
import os
import shutil

# Core paths
HOME = Path.home()
MGR_HOME = Path(os.environ.get("AGY_MGR_HOME", HOME / ".agy-manager"))
PROFILES_DIR = MGR_HOME / "profiles"
APP_PROFILES_DIR = Path(os.environ.get("AGY_APP_PROFILES_DIR", HOME / ".antigravity-profiles"))
GEMINI_HOME = HOME / ".gemini"

ACTIVE_FILE = MGR_HOME / "active.json"
STATE_FILE = MGR_HOME / "state.json"

# Antigravity artifacts & DBs
APP_CONV_DB = GEMINI_HOME / "antigravity" / "conversation_summaries.db"
CLI_CONV_DB = GEMINI_HOME / "antigravity-cli" / "conversation_summaries.db"

# Key auth files in ~/.gemini
AUTH_FILES = [
    "jetski-standalone-oauth-token",
    "oauth_creds.json",
    "google_accounts.json",
    "antigravity-cli/antigravity-oauth-token"
]

# Binaries
AGY_BIN = shutil.which("agy") or str(HOME / ".local" / "bin" / "agy")
ANTIGRAVITY_APP_PATH = Path("/Applications/Antigravity.app")
ANTIGRAVITY_APP_SUPPORT_DIR = HOME / "Library" / "Application Support" / "Antigravity"
ANTIGRAVITY_VSCDB = ANTIGRAVITY_APP_SUPPORT_DIR / "User" / "globalStorage" / "state.vscdb"
ANTIGRAVITY_APP_STORAGE = ANTIGRAVITY_APP_SUPPORT_DIR / "app_storage.json"

# Repository & Syncthing Session Sync
REPO_DIR = Path(__file__).resolve().parent.parent
SYNC_DIR = Path(os.environ.get("AGY_SYNC_DIR", REPO_DIR / "synced_sessions"))

# Ensure required directories exist
MGR_HOME.mkdir(parents=True, exist_ok=True)
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
APP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
SYNC_DIR.mkdir(parents=True, exist_ok=True)

