# SLO 延迟聚合 API — 规范与实现约定

本文件把需求中的业务规则固化为可核对的口径。实现与测试都以本文件为准。

## 1. 术语

- **服务 (service)**：被观测的线上 API 名称，必填非空字符串。
- **UTC 窗口 (window)**：左闭右开的 `[window_start, window_end)`，两端均为带时区的
  ISO-8601 时刻，落库统一转为 UTC。窗口长度必须 > 0。
- **应到实例 (expected_instances)**：由发布方声明的一个窗口内应当上报的实例名集合，
  必填、非空、无重复。实例名非空字符串。
- **桶边界 (bucket boundaries)**：单调严格递增的延迟上界（微秒），表示直方图桶的
  **右边界**。例如 `[100, 500, 1000]` 表示 `(0,100]`、`(100,500]`、`(500,1000]`、
  `>1000` 四个桶（最后一个为溢出桶，计数单独上报，不占边界）。
  - 上报字段为 `bucket_counts`，长度必须等于 `len(bucket_boundaries) + 1`；
    `bucket_counts[i]` 对应 `(boundaries[i-1], boundaries[i]]`，最后一项对应
    `(boundaries[-1], +inf)`。这是 OpenTelemetry / Prometheus 显式桶的通行约定。
  - 边界不得为负；不得重复；必须严格递增。空边界列表表示只有一个溢出桶。
- **计数 / 总和 (count / sum)**：该实例该窗口内全部观测的条数与延迟总和（微秒）。
  `count` 必须等于 `bucket_counts` 之和（自洽校验，否则 422）。
- **来源批号 (source_batch)**：实例所在的上报批次标识，由调用方提供、非空字符串。
  迟到实例进入**新批次**，旧统计按批号回查。

## 2. 幂等与冲突

提交键为 `(service, instance, window_start, window_end)`。

- **原样重传**：同键且负载的规范指纹（canonical fingerprint）完全一致 → 只收一次，
  返回 `200`，`duplicate=true`，不新增记录、不改变结果。
- **异文重传**：同键但指纹不同 → `409 Conflict`，拒绝接收，原记录保持不变。
- 指纹覆盖：实例上报的全部业务字段（边界、计数、总和、来源批号、采集时间等），
  但**不包含**服务端生成字段（接收时间、序号）。字段顺序不影响指纹。

## 3. 合并与分位数误差区间

各实例可用不同桶边界。**不能简单按边界并集分桶**：若实例 A 上报 `(100,500]`，实例 B 在
`200` 处有边界，则并集边界 `200` 会把 A 的桶劈开，而 A 的计数无法拆分；强行归到一侧
会虚构精度，甚至让区间**排除**真实值。

因此合并时按候选边界 `x`（全部实例边界的排序并集）计算合并 CDF 的**必含上下界**：

1. `cum_lower(x)`：上界 `<= x` 的实例桶计数之和——这些观测**确定** `<= x`。
2. `cum_upper(x)`：下界 `< x` 的实例桶计数之和——这些观测**可能** `<= x`。

对 `q ∈ {0.5, 0.95, 0.99}`，最近秩 `k = ceil(q * C)`（`C` 为合并总计数）：

- 只要某边界 `x` 满足 `cum_lower(x) >= k`，即证明真实分位数 `v <= x`，取最小的这类 `x` 为 `upper_us`；
- 只要某边界 `x` 满足 `cum_upper(x) < k`，即证明 `v > x`，取最大的这类 `x` 为 `lower_us`；
- 输出**闭区间** `[lower_us, upper_us]`，保证真实分位数必含其中。
- 若不存在满足 `cum_lower(x) >= k` 的边界，分位数落在溢出桶，`upper_us = null`（只有下界）。

区间宽度来自跨实例桶边界不对齐；所有实例边界完全一致时区间退化为精确桶，无额外误差。

5. **缺桶不能当零**：实例上报必须自洽（`total_count == sum(bucket_counts)`，且桶数等于
   边界数 + 1），否则 422 拒收。合并时**只合并已收齐实例**，绝不把缺失实例视为零计数。

## 4. 发布与截止时刻

- **截止时刻 (deadline)**：发布方给出的 UTC 时刻。
- 发布逻辑：对给定 `(service, window)`，只纳入 `received_at <= deadline` 的实例上报。
- `expected_instances` 中在截止时刻前没有有效上报的实例列入 `missing_instances`，
  结果状态为**待核 (pending_review)**。`unexpected_instances`（不在应到清单内）单独列出、
  不参与合并；`excluded_late_instances`（截止后才收到）单独列出、不参与本次发布。
- 状态判定（`p95_evaluation.interval_status` 记录仅按区间的原始判定）：
  - `missing_instances` 非空 → 整体 `pending_review`（待核）。
  - 否则按 p95 闭区间与目标比较：`p95.upper_us <= target` → `meets`（达标）；
    `p95.lower_us > target` → `misses`（未达标）；
    目标落在 `[lower_us, upper_us]` 内、或 `upper_us == null` → `pending_review`（待核）。
  - 无任何数据（`C == 0`）→ `pending_review`。
- 已发布结果**不可变**，持久化到 `publishes`，带自增批号 `publish_id`
  （全局单调，用 `seq` 表在同一事务内分配），可用 `GET /api/v1/published/{publish_id}` 回查。


## 5. 接口

- `POST /api/v1/reports`：接收实例上报。`201` 新建；`200` 幂等重传；`409` 异文冲突；`422` 校验失败。
- `POST /api/v1/publish`：按截止时刻发布窗口统计。`201`。
- `GET /api/v1/published/{publish_id}`：按编号回查已发布结果。
- `GET /api/v1/published`：列出已发布结果（按编号倒序，支持 `limit`）。
- `GET /api/v1/reports`：按服务/窗口/批次查询已收上报（运维排查用）。
- `GET /healthz`：存活探针。
- `GET /docs`：Swagger UI；`GET /openapi.json`：OpenAPI 文档。

## 6. 数据库

SQLite（WAL 模式），表：

- `reports`：一跳上报记录，含规范指纹、接收时间、来源批号、边界与计数（JSON）。
  唯一索引 `(service, instance, window_start, window_end)` 保证幂等键。
- `publishes`：已发布结果快照（JSON 结果体）。
- `seq`：全局发布序号。

Docker Compose 中与 API 同一镜像启动，数据卷挂载 `/data`。

## 7. 测试口径

- 单测覆盖：桶重映射、分位数区间、缺实例待核、达标/未达标、边界模糊待核、
  幂等重传、异文冲突、迟到实例新批次与旧批号回查。
- 端到端：用 `TestClient` 跑完整流程。
