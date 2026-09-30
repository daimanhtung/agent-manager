import os
import sys
from pathlib import Path

ZSH_SCRIPT = """#compdef agy-mgr

_agy_mgr() {
    local -a commands
    commands=(
        'list:Xem danh sách tài khoản'
        'switch:Chuyển đổi tài khoản đang active'
        'quota:Xem bảng theo dõi Quota thời gian thực'
        'sessions:Xem và tìm kiếm các session'
        'resume:Tiếp tục một session cũ'
        'add:Đăng nhập thêm tài khoản Google mới'
        'remove:Xóa một tài khoản'
        'import:Import tài khoản từ thư mục ngoài'
        'app:Quản lý cửa sổ Antigravity Desktop'
        'sync:Đồng bộ session giữa các thiết bị (qua Syncthing)'
        'completion:Cài đặt auto-completion cho terminal'
    )

    if (( CURRENT == 2 )); then
        _describe -t commands 'agy-mgr commands' commands
        return
    fi

    local prev="${words[2]}"
    case "$prev" in
        switch|remove|rm)
            local -a profiles
            profiles=(${(f)"$(python3 -c 'from agy_mgr.config import PROFILES_DIR; print("\\n".join([p.name for p in PROFILES_DIR.iterdir() if p.is_dir()]))' 2>/dev/null)"})
            _describe -t profiles 'tài khoản' profiles
            ;;
        sync)
            if (( CURRENT == 3 )); then
                local -a sync_cmds
                sync_cmds=(
                    'push:Xuất session ra thư mục sync'
                    'pull:Kéo session từ thư mục sync về'
                    'status:Xem trạng thái đồng bộ'
                )
                _describe -t sync_cmds 'sync actions' sync_cmds
            else
                _arguments '--id[Chỉ định session ID]' '-n[Số lượng session]' '--limit[Số lượng session]' '-f[Ghi đè]' '--force[Ghi đè]'
            fi
            ;;
        app)
            if (( CURRENT == 3 )); then
                local -a app_cmds
                app_cmds=('launch:Mở cửa sổ Antigravity với profile riêng')
                _describe -t app_cmds 'app subcommands' app_cmds
            elif (( CURRENT == 4 )) && [[ "${words[3]}" == "launch" ]]; then
                local -a profiles
                profiles=(${(f)"$(python3 -c 'from agy_mgr.config import PROFILES_DIR; print("\\n".join([p.name for p in PROFILES_DIR.iterdir() if p.is_dir()]))' 2>/dev/null)"})
                _describe -t profiles 'tài khoản' profiles
            fi
            ;;
        quota)
            _arguments '-r[Quét live quota toàn bộ]' '--refresh-all[Quét live quota toàn bộ]'
            ;;
        sessions|s)
            _arguments '-w[Lọc theo workspace]' '--workspace[Lọc theo workspace]' '-n[Số lượng]' '--limit[Số lượng]' '--search[Tìm kiếm]'
            ;;
        *)
            ;;
    esac
}

compdef _agy_mgr agy-mgr
"""

BASH_SCRIPT = """_agy_mgr_bash() {
    local cur prev words cword
    _init_completion 2>/dev/null || return

    local commands="list switch quota sessions resume sync add import remove app completion"

    if [[ $cword -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        return
    fi

    case "${words[1]}" in
        switch|remove|rm)
            local profiles=$(python3 -c "from agy_mgr.config import PROFILES_DIR; print(' '.join([p.name for p in PROFILES_DIR.iterdir() if p.is_dir()]))" 2>/dev/null)
            COMPREPLY=( $(compgen -W "$profiles" -- "$cur") )
            ;;
        sync)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "push pull status -n --limit --id -f --force" -- "$cur") )
            fi
            ;;
        quota)
            COMPREPLY=( $(compgen -W "-r --refresh-all" -- "$cur") )
            ;;
        app)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "launch" -- "$cur") )
            elif [[ $cword -eq 3 && "${words[2]}" == "launch" ]]; then
                local profiles=$(python3 -c "from agy_mgr.config import PROFILES_DIR; print(' '.join([p.name for p in PROFILES_DIR.iterdir() if p.is_dir()]))" 2>/dev/null)
                COMPREPLY=( $(compgen -W "$profiles" -- "$cur") )
            fi
            ;;
    esac
}
complete -F _agy_mgr_bash agy-mgr
"""


def get_completion_script(shell: str = "zsh") -> str:
    """Return autocompletion script for given shell."""
    if shell == "bash":
        return BASH_SCRIPT
    return ZSH_SCRIPT


def install_completion(shell: str = "zsh") -> bool:
    """Install autocompletion script into user's shell rc file."""
    home = Path.home()
    mgr_dir = home / ".agy-manager"
    mgr_dir.mkdir(parents=True, exist_ok=True)

    if shell == "zsh":
        comp_file = mgr_dir / "completion.zsh"
        comp_file.write_text(ZSH_SCRIPT, encoding="utf-8")
        rc_file = home / ".zshrc"
        line_to_add = f"source {comp_file}"
    else:
        comp_file = mgr_dir / "completion.bash"
        comp_file.write_text(BASH_SCRIPT, encoding="utf-8")
        rc_file = home / ".bashrc"
        line_to_add = f"source {comp_file}"

    if rc_file.exists():
        content = rc_file.read_text(encoding="utf-8")
        if str(comp_file) not in content:
            with open(rc_file, "a", encoding="utf-8") as f:
                f.write(f"\n# agy-mgr completion\nautoload -Uz compinit && compinit 2>/dev/null\n{line_to_add}\n")
    else:
        rc_file.write_text(f"# agy-mgr completion\nautoload -Uz compinit && compinit 2>/dev/null\n{line_to_add}\n", encoding="utf-8")

    return True
