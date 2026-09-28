import sys
from typing import List, Dict, Any, Optional

# ANSI Color codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
WHITE = "\033[37m"


def render_progress_bar(fraction: Optional[float], width: int = 12) -> str:
    """Render a colored progress bar for quota fractions (0.0 to 1.0)."""
    if fraction is None:
        return f"{DIM}[{'─' * width}] N/A{RESET}"

    pct = int(fraction * 100)
    filled_len = int(width * fraction)
    empty_len = width - filled_len

    if fraction > 0.5:
        bar_color = GREEN
    elif fraction > 0.2:
        bar_color = YELLOW
    else:
        bar_color = RED

    bar = f"{bar_color}{'█' * filled_len}{DIM}{'░' * empty_len}{RESET}"
    return f"[{bar}] {bar_color}{pct:>3}%{RESET}"


def print_header(title: str):
    print(f"\n{BOLD}{CYAN}=== {title} ==={RESET}\n")


def print_accounts_table(accounts: List[Dict[str, Any]]):
    """Print formatted accounts list."""
    print_header("DANH SÁCH TÀI KHOẢN ANTIGRAVITY")
    header = f"{BOLD}{'STT':<4} {'Trạng Thái':<12} {'Tên Profile':<18} {'Email':<30} {'Cooldown':<15}{RESET}"
    print(header)
    print("─" * 80)

    for idx, acc in enumerate(accounts, start=1):
        is_active = acc.get("is_active", False)
        status_str = f"{GREEN}★ Active{RESET}" if is_active else f"{DIM}Standby{RESET}"
        name = acc["name"]
        email = acc["email"]
        cd = acc.get("cooldown")
        cd_str = f"{YELLOW}Có (hết quota){RESET}" if cd else f"{DIM}None{RESET}"

        print(f"{idx:<4} {status_str:<21} {BOLD}{name:<18}{RESET} {email:<30} {cd_str}")
    print("─" * 80)


import re

def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for accurate length calculation."""
    return re.sub(r'\033\[[0-9;]*m', '', text)


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad string to width taking into account ANSI escape codes."""
    vis_len = len(strip_ansi(text))
    extra = max(0, width - vis_len)
    if align == "right":
        return (" " * extra) + text
    return text + (" " * extra)


def print_quota_table(quotas: List[Dict[str, Any]]):
    """Print detailed live quota status table for all accounts."""
    print_header("BẢNG THEO DÕI QUOTA TẤT CẢ TÀI KHOẢN")
    cols = [
        ("Tài khoản", 18),
        ("Trạng thái", 18),
        ("Gemini (5h)", 20),
        ("Reset 5h", 12),
        ("Gemini (Tuần)", 20),
        ("Reset Tuần", 14),
        ("Claude (Tuần)", 20),
    ]
    header = " ".join([pad(f"{BOLD}{c[0]}{RESET}", c[1]) for c in cols])
    print(header)
    print("─" * 125)

    for q in quotas:
        name = q["name"]
        if q["is_active"]:
            name_display = f"{GREEN}★ {name}{RESET}"
        else:
            name_display = f"  {name}"

        status = q["status"]
        if "Active" in status:
            status_color = f"{GREEN}{status}{RESET}"
        elif "Cooldown" in status or "Exhausted" in status:
            status_color = f"{RED}{status}{RESET}"
        elif "Cached" in status:
            status_color = f"{CYAN}{status}{RESET}"
        else:
            status_color = f"{DIM}{status}{RESET}"

        bar_5h = render_progress_bar(q.get("gemini_5h"), width=10)
        reset_5h = q.get("reset_5h") or "─"

        bar_wk = render_progress_bar(q.get("gemini_weekly"), width=10)
        reset_wk = q.get("reset_weekly") or "─"

        bar_claude = render_progress_bar(q.get("claude_weekly"), width=10)

        row = (
            f"{pad(name_display, 18)} "
            f"{pad(status_color, 18)} "
            f"{pad(bar_5h, 20)} "
            f"{pad(reset_5h, 12)} "
            f"{pad(bar_wk, 20)} "
            f"{pad(reset_wk, 14)} "
            f"{pad(bar_claude, 20)}"
        )
        print(row)

    print("─" * 125)
    print(f"{DIM}Ghi chú: [★] là tài khoản đang active. Dùng 'agy-mgr quota -r' để quét live toàn bộ.{RESET}\n")


def print_sessions_table(sessions: List[Dict[str, Any]]):
    """Print formatted list of sessions."""
    print_header("DANH SÁCH SESSION GẦN ĐÂY (APP & CLI)")
    header = f"{BOLD}{'STT':<4} {'ID':<10} {'Workspace':<18} {'Hoạt động':<14} {'Nguồn':<7} {'Tiêu đề Session':<40}{RESET}"
    print(header)
    print("─" * 100)

    for idx, s in enumerate(sessions, start=1):
        sid = s["id"][:8]
        ws = s["workspace"] or "(default)"
        if len(ws) > 16:
            ws = ws[:13] + "..."
        mtime = s["modified_str"]
        src = f"{CYAN}App{RESET}" if s["source"] == "App" else f"{MAGENTA}CLI{RESET}"
        title = s["title"]
        if len(title) > 38:
            title = title[:35] + "..."

        print(f"{idx:<4} {DIM}{sid:<10}{RESET} {ws:<18} {mtime:<14} {src:<16} {BOLD}{title}{RESET}")
    print("─" * 100)


def prompt_select_option(options: List[str], prompt: str = "Chọn số thứ tự:") -> int:
    """Prompt user to select from a list of options."""
    print(f"\n{BOLD}{prompt}{RESET}")
    for i, opt in enumerate(options, start=1):
        print(f"  {CYAN}[{i}]{RESET} {opt}")

    while True:
        try:
            choice = input(f"\nNhập lựa chọn (1-{len(options)}) hoặc 'q' để thoát: ").strip()
            if choice.lower() in ("q", "quit", "exit"):
                return -1
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return idx
            print(f"{RED}Lựa chọn không hợp lệ, vui lòng thử lại.{RESET}")
        except ValueError:
            print(f"{RED}Vui lòng nhập một số hợp lệ.{RESET}")
        except (KeyboardInterrupt, EOFError):
            print("\nĐã hủy.")
            return -1
