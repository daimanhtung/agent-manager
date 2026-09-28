import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from agy_mgr.config import PROFILES_DIR, AGY_BIN, GEMINI_HOME, MGR_HOME, AUTH_FILES
from agy_mgr.core.accounts import (
    save_profile,
    extract_email_from_gemini_dir,
    set_active_account_name,
    get_active_account_name,
    get_from_macos_keychain,
    delete_from_macos_keychain,
    sync_to_macos_keychain,
    decode_jwt_email,
)


def add_account_via_cli(name: str) -> bool:
    """
    Add a new Google account cleanly without breaking macOS Keychain.
    Flow:
    1. Backup active profile credentials and Keychain token.
    2. Temporarily clear ~/.gemini auth files and macOS Keychain entry.
    3. Run 'agy' natively so the browser opens OAuth with native macOS Keychain support (no modal errors!).
    4. Save the newly captured credentials and Keychain token as profile 'name'.
    5. If user cancels or login fails, restore the previous active account.
    """
    target_profile_dir = PROFILES_DIR / name

    # Backup currently active profile
    current_active = get_active_account_name()
    if current_active and (PROFILES_DIR / current_active).exists():
        save_profile(current_active)

    # Create safety backup of current ~/.gemini auth state
    backup_dir = MGR_HOME / ".auth_temp_backup"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    for rel_path in AUTH_FILES:
        src = GEMINI_HOME / rel_path
        if src.exists():
            dst = backup_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    current_kc = get_from_macos_keychain()
    if current_kc:
        with open(backup_dir / "keychain_token.json", "w", encoding="utf-8") as f:
            json.dump(current_kc, f, indent=2)

    print(f"\n{'='*75}")
    print(f"[*] BẮT ĐẦU ĐĂNG NHẬP TÀI KHOẢN MỚI: '{name}'")
    print(f"{'='*75}")
    print("[*] 1. Trình duyệt web sẽ tự động mở trang Google OAuth.")
    print("    2. Vui lòng chọn tài khoản Google mới và bấm 'Allow / Cho phép'.")
    print("    3. Sau khi xác thực xong trên trình duyệt, agy CLI sẽ nhận token và khởi động.")
    print("    4. Bạn chỉ cần gõ /exit hoặc nhấn Ctrl+C để hoàn tất lưu tài khoản.")
    print(f"{'='*75}\n")

    try:
        # Clear existing active tokens so agy triggers OAuth prompt
        for rel_path in AUTH_FILES:
            p = GEMINI_HOME / rel_path
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

        delete_from_macos_keychain()

        # Run agy with real HOME so macOS Keychain works natively (no popup errors)
        subprocess.call([AGY_BIN])

        # Inspect if new credentials were created
        new_kc = get_from_macos_keychain()
        new_email = None
        if new_kc and new_kc.get("id_token"):
            new_email = decode_jwt_email(new_kc["id_token"])
        if not new_email or new_email == "unknown@user":
            new_email = extract_email_from_gemini_dir(GEMINI_HOME)

        has_files = any((GEMINI_HOME / p).exists() for p in AUTH_FILES)

        if (new_kc or has_files) and (new_email and new_email != "unknown@user"):
            print(f"\n[✓] Đăng nhập thành công tài khoản: {new_email}")
            save_profile(name, gemini_dir=GEMINI_HOME, metadata_extra={"email": new_email})
            set_active_account_name(name)
            print(f"[✓] Đã lưu profile '{name}' và đặt làm tài khoản Active!")
            shutil.rmtree(backup_dir, ignore_errors=True)
            return True
        else:
            print("\n[-] Không tìm thấy token xác thực mới hoặc quá trình đăng nhập bị hủy.")
            raise RuntimeError("Auth incomplete")

    except Exception:
        print("\n[*] Đang khôi phục lại trạng thái tài khoản trước đó...")
        # Restore auth files
        for rel_path in AUTH_FILES:
            src = backup_dir / rel_path
            if src.exists():
                dst = GEMINI_HOME / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        # Restore keychain token
        kc_file = backup_dir / "keychain_token.json"
        if kc_file.exists():
            try:
                with open(kc_file, "r", encoding="utf-8") as f:
                    tok = json.load(f)
                    sync_to_macos_keychain(tok)
            except Exception:
                pass

        if current_active:
            set_active_account_name(current_active)

        print("[✓] Đã khôi phục tài khoản trước đó an toàn.")
        return False
    finally:
        shutil.rmtree(backup_dir, ignore_errors=True)
