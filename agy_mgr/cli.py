import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List

from agy_mgr.config import GEMINI_HOME
from agy_mgr.core.accounts import (
    list_accounts,
    switch_account,
    remove_account,
    get_active_account_name,
)
from agy_mgr.core.auth_flow import add_account_via_cli
from agy_mgr.core.quota import get_all_accounts_quota
from agy_mgr.core.sessions import list_all_sessions, resume_session, print_session_log
from agy_mgr.core.app_launcher import launch_app_instance, list_app_profiles
from agy_mgr.core.runner import run_with_smart_failover
from agy_mgr.core.sync import (
    sync_all,
    export_sessions,
    import_sessions,
    get_sync_status,
)
from agy_mgr.ui.formatters import (
    print_accounts_table,
    print_quota_table,
    print_sessions_table,
    print_sync_table,
    prompt_select_option,
    BOLD,
    GREEN,
    RED,
    YELLOW,
    CYAN,
    RESET,
    DIM,
)



def cmd_list(args):
    accounts = list_accounts()
    if not accounts:
        print("Chưa có tài khoản nào được đăng ký.")
        return
    print_accounts_table(accounts)


def cmd_switch(args):
    accounts = list_accounts()
    if not accounts:
        print("[!] Không có tài khoản nào trong hệ thống.")
        return

    target = args.name
    if not target:
        options = [
            f"{a['name']} ({a['email']})" + (" [Đang Active]" if a['is_active'] else "")
            for a in accounts
        ]
        selected_idx = prompt_select_option(options, prompt="Chọn tài khoản muốn chuyển sang:")
        if selected_idx < 0:
            return
        target = accounts[selected_idx]["name"]

    current = get_active_account_name()
    if target == current:
        print(f"[*] Tài khoản '{target}' hiện đã đang active.")
        return

    success = switch_account(target)
    if success:
        print(f"\n{GREEN}[+] Đã chuyển active sang tài khoản: {BOLD}{target}{RESET}")
        print(f"    (Antigravity CLI và Desktop App đã được tự động đồng bộ sang tài khoản mới)")
    else:
        print(f"\n{RED}[-] Không thể chuyển sang tài khoản '{target}'. Vui lòng kiểm tra lại tên tài khoản.{RESET}")


def cmd_quota(args):
    refresh_all = getattr(args, "refresh_all", False)
    if refresh_all:
        print("[*] Đang cập nhật live quota cho tất cả tài khoản...")
    quotas = get_all_accounts_quota(refresh_all=refresh_all)
    print_quota_table(quotas)


def cmd_sessions(args):
    limit = 0 if getattr(args, "all", False) else args.limit
    sessions = list_all_sessions(limit=limit, filter_workspace=args.workspace, search=args.search)
    if not sessions:
        print("Không tìm thấy session nào phù hợp.")
        return
    print_sessions_table(sessions)



def cmd_resume(args):
    session_id = args.id
    if not session_id:
        sessions = list_all_sessions(limit=15)
        if not sessions:
            print("Chưa có session nào được ghi nhận.")
            return

        options = [
            f"{s['id'][:8]} | [{s['workspace'] or 'Global'}] {s['title']} ({s['modified_str']})"
            for s in sessions
        ]
        selected_idx = prompt_select_option(options, prompt="Chọn session muốn tiếp tục:")
        if selected_idx < 0:
            return
        session_id = sessions[selected_idx]["id"]

    show_log = not getattr(args, "no_log", False)
    log_limit = getattr(args, "log_turns", 3)
    print(f"\n[*] Đang tiếp tục session: {CYAN}{session_id}{RESET} với agy...\n")
    resume_session(session_id, show_log=show_log, log_limit=log_limit)


def cmd_log(args):
    session_id = args.id
    if not session_id:
        sessions = list_all_sessions(limit=15)
        if not sessions:
            print("Chưa có session nào được ghi nhận.")
            return

        options = [
            f"{s['id'][:8]} | [{s['workspace'] or 'Global'}] {s['title']} ({s['modified_str']})"
            for s in sessions
        ]
        selected_idx = prompt_select_option(options, prompt="Chọn session muốn xem lịch sử chat:")
        if selected_idx < 0:
            return
        session_id = sessions[selected_idx]["id"]

    limit = None if getattr(args, "limit", 10) <= 0 else args.limit
    print_session_log(session_id, limit=limit)


def cmd_add(args):
    name = args.name.strip()
    if not name:
        print("[!] Vui lòng cung cấp tên gợi nhớ cho tài khoản (ví dụ: agy-mgr add account2)")
        return
    add_account_via_cli(name)


def cmd_remove(args):
    name = args.name.strip()
    if not name:
        print("[!] Vui lòng cung cấp tên tài khoản cần xóa.")
        return

    confirm = input(f"Bạn có chắc muốn xóa profile '{name}' không? [y/N]: ").strip().lower()
    if confirm in ("y", "yes"):
        if remove_account(name):
            print(f"[+] Đã xóa tài khoản '{name}'.")
        else:
            print(f"[-] Không tìm thấy tài khoản '{name}'.")


def cmd_app(args):
    sub = args.app_subcommand
    if sub == "launch":
        profile = args.profile
        if not profile:
            profiles = list_app_profiles()
            accounts = [a["name"] for a in list_accounts()]
            combined = sorted(list(set(profiles + accounts)))
            if not combined:
                combined = ["profile1"]
            selected_idx = prompt_select_option(combined, prompt="Chọn profile để mở cửa sổ Antigravity:")
            if selected_idx < 0:
                return
            profile = combined[selected_idx]

        print(f"\n[*] Đang khởi chạy cửa sổ Antigravity độc lập cho profile: {CYAN}{profile}{RESET}...")
        launch_app_instance(profile, workspace_path=args.workspace)
        print(f"{GREEN}[+] Cửa sổ đã được mở. Bạn có thể đăng nhập tài khoản riêng cho cửa sổ này.{RESET}")
    else:
        profiles = list_app_profiles()
        print("\n=== DANH SÁCH APP PROFILES (DESKTOP) ===")
        if not profiles:
            print("Chưa có profile nào. Dùng lệnh 'agy-mgr app launch <tên>' để mở.")
        else:
            for p in profiles:
                print(f" - {p}")
        print()


def cmd_import(args):
    name = args.name.strip()
    source_dir = Path(args.source_path).expanduser().resolve() if args.source_path else GEMINI_HOME
    if not source_dir.exists():
        print(f"[!] Thư mục nguồn {source_dir} không tồn tại.")
        return

    from agy_mgr.core.accounts import save_profile
    save_profile(name, gemini_dir=source_dir)
    print(f"{GREEN}[+] Đã import thành công profile '{name}' từ {source_dir}!{RESET}")


def cmd_completion(args):
    shell = args.shell
    if not shell:
        user_shell = os.environ.get("SHELL", "")
        shell = "zsh" if "zsh" in user_shell else "bash"

    from agy_mgr.core.completion import get_completion_script, install_completion

    if args.install:
        if install_completion(shell):
            rc = "~/.zshrc" if shell == "zsh" else "~/.bashrc"
            print(f"{GREEN}[✓] Đã cài đặt auto-completion vào {rc}!{RESET}")
            print(f"[*] Hãy chạy: {BOLD}source {rc}{RESET} hoặc mở lại terminal mới để bắt đầu dùng phím Tab gợi ý.")
        else:
            print("[-] Cài đặt completion thất bại.")
    else:
        print(get_completion_script(shell))


def cmd_sync(args):
    action = getattr(args, "sync_action", None)
    force = getattr(args, "force", False)
    limit = getattr(args, "limit", 0)
    if getattr(args, "all", False):
        limit = 0
    sess_id = getattr(args, "id", None)
    project = getattr(args, "project", None)

    if action in ("status", "st"):
        st = get_sync_status()
        print_sync_table(st)
        return

    if action in ("push", "export"):
        print(f"\n[*] Đang xuất session ra kho đồng bộ Syncthing...")
        if project:
            print(f"[*] Lọc theo project: {BOLD}{project}{RESET}")
        res = export_sessions(session_ids=[sess_id] if sess_id else None, limit=limit, force=force, project=project)
        print(f"{GREEN}[✓] Hoàn tất:{RESET} {BOLD}{res['exported']}{RESET} session được xuất, {DIM}{res['skipped']}{RESET} bỏ qua (đã mới nhất).\n")
        return

    if action in ("pull", "import"):
        print(f"\n[*] Đang nhập session từ kho đồng bộ Syncthing...")
        if project:
            print(f"[*] Lọc theo project: {BOLD}{project}{RESET}")
        res = import_sessions(session_ids=[sess_id] if sess_id else None, force=force, project=project)
        print(f"{GREEN}[✓] Hoàn tất:{RESET} {BOLD}{res['imported']}{RESET} session mới, {BOLD}{res['updated']}{RESET} đã cập nhật, {DIM}{res['skipped']}{RESET} bỏ qua.\n")
        return

    if action in ("relink", "repair", "fix"):
        print(f"\n[*] Đang quét và ánh xạ lại session theo các Project cục bộ trên máy này...")
        from agy_mgr.core.sync import relink_sessions_to_local_projects
        res = relink_sessions_to_local_projects()
        print(f"{GREEN}[✓] Hoàn tất:{RESET} Đã chuẩn hóa và liên kết {BOLD}{res['relinked']}{RESET} lượt session vào đúng Project ID và đường dẫn trên máy này!\n")
        return

    # Default: 2-way sync
    print(f"\n[*] Đang đồng bộ session 2 chiều (Syncthing / Repo)...")
    if project:
        print(f"[*] Lọc theo project: {BOLD}{project}{RESET}")
    res = sync_all(limit=limit, force=force, project=project)
    relinked_info = f" | {CYAN}{res.get('relinked', 0)}{RESET} đã ánh xạ project" if res.get('relinked') else ""
    print(
        f"{GREEN}[✓] Đồng bộ thành công:{RESET} "
        f"{BOLD}{res['imported']}{RESET} mới nhập | "
        f"{BOLD}{res['updated']}{RESET} cập nhật | "
        f"{BOLD}{res['exported']}{RESET} đã xuất"
        f"{relinked_info} | "
        f"{DIM}{res['skipped']}{RESET} bỏ qua\n"
    )



def cmd_update(args):
    repo_dir = Path(__file__).resolve().parent.parent
    print(f"\n[*] Đang cập nhật agy-mgr từ GitHub...")
    if (repo_dir / ".git").exists():
        res = subprocess.call(["git", "-C", str(repo_dir), "pull"])
        if res == 0:
            print(f"\n{GREEN}[✓] Cập nhật thành công agy-mgr lên phiên bản mới nhất!{RESET}\n")
            return

    # Fallback to curl installer
    subprocess.call("curl -fsSL https://raw.githubusercontent.com/daimanhtung/agent-manager/main/install.sh | bash", shell=True)



def main():
    parser = argparse.ArgumentParser(
        prog="agy-mgr",
        description="Bộ công cụ quản lý Multi-Account, Quota & Session cho Google Antigravity (agy)",
    )
    subparsers = parser.add_subparsers(dest="command")

    # list
    p_list = subparsers.add_parser("list", aliases=["ls"], help="Liệt kê tất cả tài khoản")
    p_list.set_defaults(func=cmd_list)

    # switch
    p_switch = subparsers.add_parser("switch", aliases=["sw"], help="Chuyển đổi tài khoản active")
    p_switch.add_argument("name", nargs="?", help="Tên profile tài khoản muốn chuyển sang")
    p_switch.set_defaults(func=cmd_switch)

    # quota
    p_quota = subparsers.add_parser("quota", aliases=["q"], help="Xem bảng Quota của tất cả tài khoản")
    p_quota.add_argument("--refresh-all", "-r", action="store_true", help="Quét và cập nhật live quota cho toàn bộ tài khoản")
    p_quota.set_defaults(func=cmd_quota)

    # sessions
    p_sess = subparsers.add_parser("sessions", aliases=["s", "history"], help="Xem danh sách session gần đây")
    p_sess.add_argument("--limit", "-n", type=int, default=50, help="Số lượng session hiển thị (mặc định: 50, 0 = toàn bộ)")
    p_sess.add_argument("--all", "-a", action="store_true", help="Hiển thị tất cả session không giới hạn")
    p_sess.add_argument("--workspace", "-w", help="Lọc theo thư mục workspace")
    p_sess.add_argument("--search", help="Tìm kiếm session theo từ khóa")
    p_sess.set_defaults(func=cmd_sessions)


    # resume
    p_res = subparsers.add_parser("resume", aliases=["r"], help="Tiếp tục (resume) session và xem lại ngữ cảnh cũ")
    p_res.add_argument("id", nargs="?", help="ID session (nếu để trống sẽ hiển thị menu chọn)")
    p_res.add_argument("--no-log", action="store_true", help="Không in lại lịch sử trước khi vào")
    p_res.add_argument("--log-turns", "-n", type=int, default=3, help="Số lượt hội thoại cũ in ra xem lại trước khi vào (mặc định: 3)")
    p_res.set_defaults(func=cmd_resume)

    # log / view
    p_log = subparsers.add_parser("log", aliases=["view", "show"], help="Xem lại lịch sử hội thoại (transcript) của session")
    p_log.add_argument("id", nargs="?", help="ID session (nếu để trống sẽ hiển thị menu chọn)")
    p_log.add_argument("--limit", "-n", type=int, default=10, help="Số lượt hội thoại muốn xem (mặc định: 10, 0 = toàn bộ)")
    p_log.set_defaults(func=cmd_log)

    # sync
    p_sync = subparsers.add_parser("sync", help="Đồng bộ session giữa các thiết bị (qua Syncthing / Repo)")
    p_sync.add_argument("sync_action", nargs="?", choices=["push", "export", "pull", "import", "status", "st", "relink", "repair", "fix"], help="Hành động: push (xuất), pull (nhập), status (kiểm tra), relink (ánh xạ lại project). Mặc định là đồng bộ 2 chiều")
    p_sync.add_argument("--id", help="Chỉ đồng bộ một session ID cụ thể")
    p_sync.add_argument("--limit", "-n", type=int, default=0, help="Số session gần nhất cần đồng bộ (mặc định: 0 = toàn bộ)")
    p_sync.add_argument("--all", "-a", action="store_true", help="Đồng bộ toàn bộ session không giới hạn số lượng")
    p_sync.add_argument("--project", "-p", help="Lọc đồng bộ theo tên project hoặc workspace (ví dụ: devops)")
    p_sync.add_argument("--force", "-f", action="store_true", help="Ghi đè ngay cả khi bản hiện tại bằng hoặc mới hơn")
    p_sync.set_defaults(func=cmd_sync)


    # add
    p_add = subparsers.add_parser("add", help="Thêm tài khoản Google mới")
    p_add.add_argument("name", help="Tên đại diện cho tài khoản (ví dụ: work, acc2)")
    p_add.set_defaults(func=cmd_add)

    # import
    p_imp = subparsers.add_parser("import", help="Import tài khoản từ thư mục .gemini khác")
    p_imp.add_argument("name", help="Tên profile muốn đặt")
    p_imp.add_argument("source_path", nargs="?", help="Đường dẫn thư mục nguồn (mặc định ~/.gemini)")
    p_imp.set_defaults(func=cmd_import)

    # remove
    p_rem = subparsers.add_parser("remove", aliases=["rm"], help="Xóa tài khoản khỏi danh bạ")
    p_rem.add_argument("name", help="Tên tài khoản cần xóa")
    p_rem.set_defaults(func=cmd_remove)

    # app
    p_app = subparsers.add_parser("app", help="Quản lý cửa sổ Antigravity Desktop App")
    app_subs = p_app.add_subparsers(dest="app_subcommand")
    p_app_launch = app_subs.add_parser("launch", help="Mở cửa sổ Antigravity độc lập")
    p_app_launch.add_argument("profile", nargs="?", help="Tên profile độc lập")
    p_app_launch.add_argument("--workspace", "-w", help="Đường dẫn thư mục project để mở")
    p_app.set_defaults(func=cmd_app)

    # completion
    p_comp = subparsers.add_parser("completion", help="Cài đặt auto-completion gợi ý lệnh trong terminal")
    p_comp.add_argument("shell", nargs="?", choices=["zsh", "bash"], help="Loại shell (zsh hoặc bash)")
    p_comp.add_argument("--install", "-i", action="store_true", help="Tự động cài đặt cấu hình vào .zshrc hoặc .bashrc")
    p_comp.set_defaults(func=cmd_completion)

    # update
    p_up = subparsers.add_parser("update", aliases=["up"], help="Cập nhật agy-mgr lên phiên bản mới nhất từ GitHub")
    p_up.set_defaults(func=cmd_update)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


def run_cli_wrapper():
    """Entry point for `agy-run` command with smart auto-balance & failover."""
    raw_args = sys.argv[1:]
    exit_code = run_with_smart_failover(raw_args, strategy="highest_quota")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
