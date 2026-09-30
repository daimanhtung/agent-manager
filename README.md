# Antigravity Multi-Account, Quota & Session Manager (`agy-mgr`)

Bộ công cụ mạnh mẽ và tiện lợi giúp quản trị đa tài khoản, theo dõi quota thời gian thực, quản lý và tiếp tục các session làm việc cho **Google Antigravity (`agy` CLI & Antigravity Desktop App)**.

---

## 🌟 Tính Năng Nổi Bật

1. **Quản Lý Đa Tài Khoản & Chuyển Đổi Nhanh:**
   - Hỗ trợ thêm nhiều tài khoản Google thông qua OAuth chính chủ (`agy-mgr add <name>`).
   - Chuyển đổi tài khoản active chỉ trong 1 giây (`agy-mgr switch [name]`).
   - Tự động đồng bộ sang cả Antigravity CLI và Antigravity Desktop App.
2. **Theo Dõi Quota Thời Gian Thực (Unified Quota Tracker):**
   - Xem % quota còn lại của tất cả tài khoản với thanh tiến độ trực quan (`[████████░░] 89%`).
   - Hiển thị cả hạn mức ngắn hạn (5 giờ) và dài hạn (tuần) cho Gemini Models và Claude/GPT.
   - Hiển thị thời gian đếm ngược reset quota.
3. **Quản Lý Session Tập Trung (Unified Session Manager):**
   - Tổng hợp toàn bộ session từ cả **Antigravity App** và **`agy` CLI** theo thời gian thực.
   - Tìm kiếm session theo tên, từ khóa hoặc thư mục workspace.
   - **1-Click Resume:** Menu tương tác chọn session để mở lại tiếp tục làm việc ngay lập tức.
4. **Đồng Bộ Session Đa Thiết Bị (Syncthing / Repo Sync):**
   - Tự động đồng bộ toàn bộ lịch sử session, tóm tắt và brain transcript giữa nhiều máy tính.
   - Tận dụng cơ chế đồng bộ Syncthing của chính repository này: an toàn, tức thì, bảo mật P2P.
   - Tự động nhận diện và cập nhật session khi chạy `agy-mgr sessions` hoặc `agy-mgr resume`.
5. **Smart Runner (`agy-run`) & Tự Động Chuyển Khi Hết Quota:**
   - Thay thế lệnh gọi `agy` thông thường bằng `agy-run`.
   - Tự động chọn tài khoản có quota cao nhất (Highest-Quota-First).
   - Tự động bắt lỗi `429 / RESOURCE_EXHAUSTED` và chuyển sang tài khoản kế tiếp mà không ngắt quãng công việc.
6. **Chạy Song Song Nhiều Cửa Sổ Antigravity Desktop App:**
   - Mở đồng thời 2 hoặc nhiều cửa sổ Antigravity App độc lập, mỗi cửa sổ đăng nhập một tài khoản riêng biệt.


---

## 🚀 Cài Đặt Nhanh 1 Dòng (Dành cho máy mới)

Chỉ cần chạy lệnh bash sau trên bất kỳ máy nào (macOS / Linux):

```bash
curl -fsSL https://raw.githubusercontent.com/daimanhtung/agent-manager/main/install.sh | bash
```

Hoặc nếu đã clone mã nguồn về máy:
```bash
./install.sh
```

Lệnh cài đặt sẽ:
1. Tự động kiểm tra môi trường Python 3.
2. Tải và liên kết các lệnh `agy-mgr` và `agy-run` vào `~/.local/bin/`.
3. Tự động cấu hình `PATH` trong file shell (`.zshrc` hoặc `.bashrc`).
4. Khởi tạo và nhận diện tài khoản hiện có.

---

## 📖 Hướng Dẫn Sử Dụng Chi Tiết

### 1. Quản lý Tài khoản

```bash
# Xem danh sách tất cả tài khoản và tài khoản đang Active
agy-mgr list

# Thêm một tài khoản Google mới (Trình duyệt sẽ tự mở trang OAuth Google)
agy-mgr add acc2

# Chuyển đổi tài khoản đang kích hoạt (Hiển thị menu tương tác để chọn)
agy-mgr switch

# Hoặc chuyển đổi trực tiếp bằng tên
agy-mgr switch acc2

# Xóa một tài khoản khỏi danh bạ
agy-mgr remove acc2
```

### 2. Theo dõi Quota Tất Cả Tài Khoản

```bash
agy-mgr quota
```
*Kết quả mẫu:*
```text
=== BẢNG THEO DÕI QUOTA TẤT CẢ TÀI KHOẢN ===

Tài khoản          Trạng thái       Gemini (5h)                  Gemini (Tuần)                Reset vào      
─────────────────────────────────────────────────────────────────────────────────────────────────────────
★ fruitninja2909   Active (Live)    [██████████░░]  89%          [███████░░░░░]  64%          3h 0m
  acc2             Standby          [████████████] 100%          [████████████] 100%          Sẵn sàng
─────────────────────────────────────────────────────────────────────────────────────────────────────────
```

### 3. Quản lý & Tiếp tục Session Làm Việc

```bash
# Xem 20 session gần nhất từ cả App và CLI
agy-mgr sessions

# Tìm kiếm session theo từ khóa hoặc workspace
agy-mgr sessions --search "bot cờ tướng"
agy-mgr sessions --workspace "agent-manager"

# Tiếp tục session (Hiển thị menu chọn session)
agy-mgr resume

# Hoặc tiếp tục trực tiếp theo ID session
agy-mgr resume 128ccf9a
```

### 4. Đồng Bộ Session Giữa Các Thiết Bị (Syncthing / Repo)

Khi bạn làm việc trên nhiều máy tính (Laptop, PC, Mac mini Server) và repository này đã được cấu hình **Syncthing** đồng bộ qua lại:

```bash
# Đồng bộ 2 chiều (nhập session mới từ máy khác và xuất session máy hiện tại)
agy-mgr sync

# Xem bảng tình trạng đồng bộ giữa máy hiện tại và kho sync
agy-mgr sync status

# Chỉ đẩy session máy này ra kho sync
agy-mgr sync push

# Chỉ kéo session từ kho sync vào máy này
agy-mgr sync pull

# Đồng bộ một session cụ thể theo ID
agy-mgr sync --id fec07da5
```

> **Cơ chế tự động:** Khi bạn chạy `agy-mgr sessions` hoặc `agy-mgr resume`, công cụ sẽ **tự động kiểm tra và nhận diện** các session mới mà Syncthing vừa truyền về từ các máy khác mà bạn không cần phải gõ lệnh kéo thủ công!

### 5. Smart Runner (`agy-run`)

Sử dụng `agy-run` thay cho `agy`. Tool sẽ tự động ưu tiên tài khoản nhiều quota nhất và tự động failover nếu gặp giới hạn rate limit:

```bash
# Chạy prompt chế độ print
agy-run -p "Viết một hàm Python tính fibonacci"

# Chạy phiên tương tác
agy-run -i "Tiếp tục review code"
```

### 6. Mở Cửa Sổ Antigravity Desktop App Độc Lập (Multi-Window)

Để mở thêm một cửa sổ Antigravity App hoàn toàn riêng biệt (chạy song song trên màn hình với tài khoản khác):
```bash
# Mở cửa sổ cho profile chỉ định
agy-mgr app launch acc2

# Mở cửa sổ kèm thư mục project cụ thể
agy-mgr app launch acc2 --workspace /Users/daitung/Project/MyProject/agent-manager
```

---

## 📂 Cấu Trúc Lưu Trữ Dữ Liệu
- `synced_sessions/`: Kho chứa các session đồng bộ giữa các máy (được Syncthing tự động đồng bộ qua P2P).
- `~/.agy-manager/profiles/`: Chứa các profile tài khoản độc lập.
- `~/.agy-manager/active.json`: Ghi nhận tài khoản đang active.
- `~/.agy-manager/state.json`: Lưu trữ trạng thái cooldown và bộ đếm.
- `~/.antigravity-profiles/`: Chứa dữ liệu các instance Antigravity App độc lập.

