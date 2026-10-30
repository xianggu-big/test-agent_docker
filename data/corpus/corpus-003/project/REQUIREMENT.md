# 会议室预约 API 需求（corpus-003）

1. 创建会议室 `POST /rooms`：`name` 长度 1~20，`capacity` 1~20 整数，`hourly_rate` ≥1；
   成功返回 201 与 `id/name/capacity/hourly_rate`。
2. 按 id 查询会议室 `GET /rooms/{rid}`：存在返回 200；不存在返回 404。
3. 创建预约 `POST /bookings`：
   - `room_id` 必须存在（否则 404）；
   - `date` 为 `YYYY-MM-DD`；`start_hour` 8~20，`end_hour` 9~21；
   - 预约时长必须在 1~4 小时闭区间内（否则 400）；
   - 同一会议室同一天的两个时段重叠（前场结束 == 后场开始不算重叠）应返回 409；
   - 成功返回 201，包含 `id/room_id/date/start_hour/end_hour/total/status`，
     `status` 初始为 `active`；预约必须持久化，创建后可按 id 查询。
4. 按 id 查询预约 `GET /bookings/{bid}`：存在返回 200；不存在返回 404。
5. 取消预约 `POST /bookings/{bid}/cancel`：存在且 `active` → 200 且 `status=cancelled`；
   不存在 → 404；已取消再次取消 → 409。
6. 列出某会议室某日预约 `GET /rooms/{rid}/bookings?date=YYYY-MM-DD`：
   存在会议室返回 200 与 `items` 列表（仅 active）；会议室不存在 → 404。

计价：总价 `total` = `hourly_rate × 时长`；时长 ≥3 小时打 9 折（总价 × 0.9），
结果四舍五入到整数。
