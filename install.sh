#!/usr/bin/env bash
# ==============================================================================
# agy-mgr & agy-run: One-line Quick Installer
# Compatible with macOS & Linux (zsh / bash)
# ==============================================================================

set -e

REPO_URL="https://github.com/daimanhtung/agent-manager.git"
INSTALL_DIR="$HOME/.agy-manager/app"
BIN_DIR="$HOME/.local/bin"

# ANSI Colors
BOLD="\033[1m"
GREEN="\033[32m"
CYAN="\033[36m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

echo -e "\n${BOLD}${CYAN}=== Cài Đặt Antigravity Multi-Account Manager (agy-mgr) ===${RESET}\n"

# 1. Check Python 3
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}[!] Không tìm thấy Python 3. Vui lòng cài đặt Python 3 trước khi tiếp tục.${RESET}"
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "${GREEN}[+] Phát hiện Python: ${PY_VER}${RESET}"

# 2. Determine target repository directory
# If this script is run from inside a cloned repo directory, use it
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/agy_mgr/cli.py" ]; then
    TARGET_DIR="$SCRIPT_DIR"
    echo -e "${GREEN}[+] Sử dụng mã nguồn tại: ${TARGET_DIR}${RESET}"
else
    # Otherwise clone or update in ~/.agy-manager/app
    mkdir -p "$HOME/.agy-manager"
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo -e "${CYAN}[*] Cập nhật mã nguồn mới nhất từ GitHub...${RESET}"
        git -C "$INSTALL_DIR" pull --ff-only || true
    else
        echo -e "${CYAN}[*] Tải mã nguồn về ${INSTALL_DIR}...${RESET}"
        rm -rf "$INSTALL_DIR"
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    TARGET_DIR="$INSTALL_DIR"
fi

# 3. Create ~/.local/bin and symlinks
mkdir -p "$BIN_DIR"
chmod +x "$TARGET_DIR/bin/agy-mgr" "$TARGET_DIR/bin/agy-run"

ln -sf "$TARGET_DIR/bin/agy-mgr" "$BIN_DIR/agy-mgr"
ln -sf "$TARGET_DIR/bin/agy-run" "$BIN_DIR/agy-run"
echo -e "${GREEN}[+] Đã liên kết lệnh: ${BIN_DIR}/agy-mgr & ${BIN_DIR}/agy-run${RESET}"

# 3b. Auto-detect AGY_SYNC_DIR if TARGET_DIR is outside a Syncthing-watched folder
# This ensures 'agy-mgr sync push' writes to a folder that Syncthing can sync
DETECTED_SYNC_DIR=""
# Look for an agent-manager repo inside common project folders that Syncthing watches
for candidate_repo in \
    "$HOME/Project/MyProject/agent-manager" \
    "$HOME/Projects/MyProject/agent-manager" \
    "$HOME/Documents/MyProject/agent-manager" \
    "$HOME/workspace/agent-manager"; do
    if [ -f "$candidate_repo/agy_mgr/cli.py" ] && [ "$candidate_repo" != "$TARGET_DIR" ]; then
        DETECTED_SYNC_DIR="$candidate_repo/synced_sessions"
        echo -e "${CYAN}[*] Phát hiện Syncthing repo tại: ${candidate_repo}${RESET}"
        break
    fi
done

# 4. Ensure ~/.local/bin is in PATH
SHELL_CONFIG=""
if [ -n "$ZSH_VERSION" ] || [ "$SHELL" = "/bin/zsh" ] || [ "$SHELL" = "/usr/bin/zsh" ]; then
    SHELL_CONFIG="$HOME/.zshrc"
elif [ -f "$HOME/.bash_profile" ]; then
    SHELL_CONFIG="$HOME/.bash_profile"
else
    SHELL_CONFIG="$HOME/.bashrc"
fi

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "${YELLOW}[*] Đang thêm ${BIN_DIR} vào PATH trong ${SHELL_CONFIG}...${RESET}"
    echo -e '\n# Antigravity Manager PATH\nexport PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_CONFIG"
    export PATH="$BIN_DIR:$PATH"
fi

# 4b. Write AGY_SYNC_DIR to shell config if we detected a Syncthing-watched repo
if [ -n "$DETECTED_SYNC_DIR" ]; then
    if ! grep -q "AGY_SYNC_DIR" "$SHELL_CONFIG" 2>/dev/null; then
        echo -e "${CYAN}[*] Ghi AGY_SYNC_DIR vào ${SHELL_CONFIG} để sync qua Syncthing...${RESET}"
        echo -e "\n# agy-mgr: trỏ synced_sessions vào folder được Syncthing watch\nexport AGY_SYNC_DIR=\"$DETECTED_SYNC_DIR\"" >> "$SHELL_CONFIG"
        export AGY_SYNC_DIR="$DETECTED_SYNC_DIR"
        echo -e "${GREEN}[+] AGY_SYNC_DIR=${DETECTED_SYNC_DIR}${RESET}"
    else
        echo -e "${YELLOW}[~] AGY_SYNC_DIR đã được cấu hình trong ${SHELL_CONFIG}, bỏ qua.${RESET}"
    fi
fi

# 5. Initialize active profile & shell completion
python3 -c "from agy_mgr.core.accounts import auto_import_current_if_empty; auto_import_current_if_empty()" 2>/dev/null || true
"$BIN_DIR/agy-mgr" completion --install >/dev/null 2>&1 || true

# 6. Success message
echo -e "\n${BOLD}${GREEN}======================================================${RESET}"
echo -e "${BOLD}${GREEN} CÀI ĐẶT THÀNH CÔNG!${RESET}"
echo -e "${BOLD}${GREEN}======================================================${RESET}\n"
echo -e "Bạn có thể sử dụng ngay các lệnh sau:"
echo -e "  ${CYAN}agy-mgr list${RESET}          : Xem danh sách tài khoản"
echo -e "  ${CYAN}agy-mgr quota${RESET}         : Xem bảng theo dõi Quota thời gian thực"
echo -e "  ${CYAN}agy-mgr sessions${RESET}      : Quản lý và xem danh sách session"
echo -e "  ${CYAN}agy-mgr switch${RESET}        : Chuyển đổi tài khoản nhanh"
echo -e "  ${CYAN}agy-mgr add <tên>${RESET}     : Đăng nhập thêm tài khoản mới"
echo -e "  ${CYAN}agy-run <lệnh>${RESET}        : Chạy agy với cơ chế tự chuyển khi hết Quota"
echo -e ""
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "${YELLOW}Lưu ý:${RESET} Hãy chạy ${BOLD}source ${SHELL_CONFIG}${RESET} hoặc mở lại terminal mới để cập nhật PATH.\n"
fi
