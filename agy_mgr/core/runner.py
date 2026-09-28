import os
import subprocess
import sys
import time
from typing import List, Optional

from agy_mgr.config import AGY_BIN
from agy_mgr.core.accounts import (
    list_accounts,
    get_active_account_name,
    switch_account,
    set_account_cooldown,
)
from agy_mgr.core.quota import get_all_accounts_quota

QUOTA_ERROR_PATTERNS = [
    "resource_exhausted",
    "quota exhausted",
    "allocation quota reached",
    "rate limit",
    "429 too many requests",
    "429",
    "verify your account to continue",
]


def is_quota_error(text: str) -> bool:
    """Check if output contains quota exhaustion or rate limit signals."""
    lowered = text.lower()
    return any(p in lowered for p in QUOTA_ERROR_PATTERNS)


def select_best_account(strategy: str = "highest_quota") -> Optional[str]:
    """Select the best account based on strategy and current quota/cooldowns."""
    accounts = get_all_accounts_quota()
    # Filter out accounts currently in cooldown
    available = [a for a in accounts if not a.get("cooldown")]

    if not available:
        return None

    current_active = get_active_account_name()

    if strategy == "highest_quota":
        # Sort by gemini_weekly quota (descending), None treated as 1.0 (untested/available)
        def score(acc):
            frac = acc.get("gemini_weekly")
            return 1.0 if frac is None else frac
        available.sort(key=score, reverse=True)
        return available[0]["name"]

    elif strategy == "round_robin":
        names = [a["name"] for a in available]
        if current_active in names:
            idx = (names.index(current_active) + 1) % len(names)
            return names[idx]
        return names[0]

    # Default fallback: keep current if available
    for a in available:
        if a["name"] == current_active:
            return current_active
    return available[0]["name"]


def run_with_smart_failover(args: List[str], strategy: str = "highest_quota") -> int:
    """
    Execute `agy` with given arguments.
    Automatically catches quota exhaustion errors and fails over to another account.
    """
    accounts = list_accounts()
    if not accounts:
        print("[!] Chưa có tài khoản nào được đăng ký trong agy-mgr.")
        print("    Vui lòng chạy: agy-mgr add <tên-tài-khoản>")
        return 1

    # Check if this is an interactive session or print mode
    is_print_mode = any(arg in args for arg in ["-p", "--print", "--prompt"])

    # If interactive (no -p), directly run with best account
    if not is_print_mode:
        best = select_best_account(strategy)
        if best and best != get_active_account_name():
            print(f"[*] Chuyển sang tài khoản tối ưu: {best}")
            switch_account(best)
        cmd = [AGY_BIN] + args
        try:
            return subprocess.call(cmd)
        except KeyboardInterrupt:
            return 130

    # In print / non-interactive mode, monitor stdout/stderr for quota exhaustion
    max_attempts = len(accounts)
    attempt = 0

    while attempt < max_attempts:
        attempt += 1
        current_acc = get_active_account_name() or accounts[0]["name"]
        cmd = [AGY_BIN] + args

        print(f"[*] Đang thực thi với tài khoản: [ {current_acc} ] ...")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        output_lines = []
        exhausted = False

        try:
            for line in proc.stdout:
                output_lines.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
                if is_quota_error(line):
                    exhausted = True
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return 130

        # Check exit code or error output
        full_output = "".join(output_lines)
        if proc.returncode != 0 or exhausted or is_quota_error(full_output):
            if is_quota_error(full_output) or proc.returncode in (3, 429):
                print(f"\n[!] CẢNH BÁO: Tài khoản '{current_acc}' đã hết quota hoặc bị Rate Limit.")
                set_account_cooldown(current_acc, duration_seconds=1800, reason="Quota limit reached")

                # Find next best account
                next_acc = select_best_account(strategy)
                if next_acc and next_acc != current_acc:
                    print(f"[+] Tự động đổi sang tài khoản kế tiếp: [ {next_acc} ]\n")
                    switch_account(next_acc)
                    continue
                else:
                    print("[-] Tất cả tài khoản khả dụng đều đã hết Quota hoặc đang Cooldown.")
                    print("    Dùng lệnh 'agy-mgr quota' để xem thời gian hồi phục của từng tài khoản.")
                    return proc.returncode
        return proc.returncode

    return 0
