# agent-manager (agy-mgr) ⚡ — Mission & Goal Specification

- **Mục tiêu Tối Thượng (North Star Goal):** Bộ công cụ quản trị đa tài khoản Google Antigravity, tự động luân chuyển quota thời gian thực, quản lý và tiếp tục session làm việc P2P (Syncthing), hỗ trợ đa cửa sổ Desktop App và smart runner `agy-run`.
- **Tech Stack:** Python 3, Shell, Syncthing P2P, JSON State Store
- **Phase hiện tại:** Phase 4: User Analytics & Device Sync Optimization
- **Tiến độ:** 85%
- **Vai trò thực hiện:** Growth
- **Task kế tiếp:** Kiểm tra độ trễ đồng bộ session qua Syncthing giữa Mac mini và Laptop, tối ưu logic Highest-Quota-First

---

## 1. Lộ Trình Phát Triển (Phased Milestones)
- **Phase 1: Core Architecture & CLI [DONE]:**
  - Quản lý đa account OAuth Google, switch profile 1s, live quota tracker.
- **Phase 2: QA & Multi-Window Stability [DONE]:**
  - Khởi chạy nhiều instance Antigravity Desktop độc lập, catch lỗi rate limit 429.
- **Phase 3: Cross-Device Syncthing Sync [DONE]:**
  - Đồng bộ 2 chiều session qua Syncthing an toàn, bảo mật P2P.
- **Phase 4: Telemetry & Quota Efficiency [IN PROGRESS]:**
  - Thống kê thời gian sống của session, tổng lượng token tiết kiệm được nhờ luân chuyển tài khoản.
- **Phase 5: Marketing & Open Source Distribution:**
  - Hướng dẫn cài đặt 1-dòng cho cộng đồng developer dùng Google Antigravity.

---

## 2. Tiêu Chuẩn Hoàn Thành (Definition of Done - DoD)
- [x] BA hoàn thiện đặc tả luân chuyển tài khoản và cấu trúc lưu session
- [x] Dev hoàn thiện `agy-mgr` và `agy-run` CLI
- [x] Tester xác nhận chuyển đổi tài khoản không làm mất context session
- [x] Audit pass bảo mật OAuth token trong `~/.agy-manager/profiles/`
- [x] Cơ chế Syncthing P2P hoạt động tự động giữa các máy trạm
- [ ] Báo cáo hiệu suất sử dụng token và tỷ lệ failover 429
- [ ] Bản hướng dẫn README v2 hoàn chỉnh

---

## 3. Kế Hoạch Đo Lường & Tăng Trưởng (Analytics & Marketing)
- **North Star Metric:** Tỷ lệ phiên làm việc không bị gián đoạn do hết quota (Zero-downtime AI Sessions).
- **Kênh tiếp cận:** Cộng đồng AI Engineers và Developers sử dụng Antigravity.
