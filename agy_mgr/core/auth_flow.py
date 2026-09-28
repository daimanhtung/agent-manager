import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from agy_mgr.config import PROFILES_DIR, AGY_BIN, GEMINI_HOME
from agy_mgr.core.accounts import save_profile, extract_email_from_gemini_dir, set_active_account_name


def add_account_via_cli(name: str) -> bool:
    """
    Add a new Google account by spawning a clean agy session in a temporary HOME.
    agy will launch browser OAuth, write tokens to the temp dir, and we import them into profiles.
    """
    target_profile_dir = PROFILES_DIR / name
    if target_profile_dir.exists():
        print(f"[!] Tài khoản '{name}' đã tồn tại. Nếu muốn đăng nhập lại, hãy xóa trước bằng 'agy-mgr remove {name}'.")
        return False

    temp_home = PROFILES_DIR / f".tmp_login_{name}_{int(time.time())}"
    temp_home.mkdir(parents=True, exist_ok=True)

    print(f"\n[*] Đang khởi tạo phiên đăng nhập mới cho '{name}'...")
    print("[*] Trình duyệt sẽ tự động mở trang đăng nhập Google.")
    print("    Vui lòng chọn tài khoản Google mới và bấm 'Allow/Cho phép'...\n")

    env = os.environ.copy()
    env["HOME"] = str(temp_home)

    cmd = [AGY_BIN, "-p", "Say 'Login success' in 3 words"]
    try:
        # Run agy in the temporary home
        proc = subprocess.run(cmd, env=env)
        temp_gemini = temp_home / ".gemini"
        if temp_gemini.exists():
            # Check if tokens were created
            email = extract_email_from_gemini_dir(temp_gemini)
            if email and email != "unknown@user":
                print(f"\n[+] Đăng nhập thành công tài khoản: {email}")
                save_profile(name, gemini_dir=temp_gemini, metadata_extra={"email": email})
                set_active_account_name(name)
                print(f"[+] Đã lưu profile '{name}' và đặt làm tài khoản active!")
                return True
            else:
                # Still check if auth files exist
                save_profile(name, gemini_dir=temp_gemini)
                set_active_account_name(name)
                print(f"[+] Đã lưu profile '{name}'!")
                return True
        else:
            print("[-] Không tìm thấy dữ liệu xác thực sau khi hoàn thành.")
            return False
    except KeyboardInterrupt:
        print("\n[!] Đã hủy quá trình đăng nhập.")
        return False
    finally:
        if temp_home.exists():
            shutil.rmtree(temp_home, ignore_errors=True)
