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
4. **Smart Runner (`agy-run`) & Tự Động Chuyển Khi Hết Quota:**
   - Thay thế lệnh gọi `agy` thông thường bằng `agy-run`.
   - Tự động chọn tài khoản có quota cao nhất (Highest-Quota-First).
   - Tự động bắt lỗi `429 / RESOURCE_EXHAUSTED` và chuyển sang tài khoản kế tiếp mà không ngắt quãng công việc.
5. **Chạy Song Song Nhiều Cửa Sổ Antigravity Desktop App:**
   - Mở đồng thời 2 hoặc nhiều cửa sổ Antigravity App độc lập, mỗi cửa sổ đăng nhập một tài khoản riêng biệt.

---

## 🚀 Cài Đặt & Khởi Tạo

Các lệnh `agy-mgr` và `agy-run` đã được liên kết trực tiếp vào `~/.local/bin/` (đã nằm trong `$PATH` của bạn).

Bạn có thể gọi ngay ở bất kỳ terminal nào:
```bash
agy-mgr --help
agy-run --help
```

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

### 4. Smart Runner (`agy-run`)

Sử dụng `agy-run` thay cho `agy`. Tool sẽ tự động ưu tiên tài khoản nhiều quota nhất và tự động failover nếu gặp giới hạn rate limit:

```bash
# Chạy prompt chế độ print
agy-run -p "Viết một hàm Python tính fibonacci"

# Chạy phiên tương tác
agy-run -i "Tiếp tục review code"
```

### 5. Mở Cửa Sổ Antigravity Desktop App Độc Lập (Multi-Window)

Để mở thêm một cửa sổ Antigravity App hoàn toàn riêng biệt (chạy song song trên màn hình với tài khoản khác):
```bash
# Mở cửa sổ cho profile chỉ định
agy-mgr app launch acc2

# Mở cửa sổ kèm thư mục project cụ thể
agy-mgr app launch acc2 --workspace /Users/daitung/Project/MyProject/agent-manager
```

---

## 📂 Cấu Trúc Lưu Trữ Dữ Liệu
- `~/.agy-manager/profiles/`: Chứa các profile tài khoản độc lập.
- `~/.agy-manager/active.json`: Ghi nhận tài khoản đang active.
- `~/.agy-manager/state.json`: Lưu trữ trạng thái cooldown và bộ đếm.
- `~/.antigravity-profiles/`: Chứa dữ liệu các instance Antigravity App độc lập.
