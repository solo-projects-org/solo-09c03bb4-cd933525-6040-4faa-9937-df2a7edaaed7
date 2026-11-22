# 延迟 SLO 评审 API（FastAPI + SQLite）

接收各实例上报的延迟直方图，按 UTC 窗口和截止时刻合并发布 p50/p95/p99 的**必含误差区间**，
并对 p95 目标给出**达标 / 未达标 / 待核**三态结论。业务口径见 [`SPEC.md`](SPEC.md)。

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
curl -s http://127.0.0.1:8000/healthz
```

- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI JSON：<http://127.0.0.1:8000/openapi.json>
- 数据持久化在具名卷 `slo-data`（容器内 `/data/slo.db`）。

跑一遍端到端自测（含幂等、冲突、缺实例、迟到批次、旧批号回查）：

```bash
docker compose run --rm selfcheck
```

本机无 Docker 时可直接跑：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
SLO_DB_PATH=/tmp/slo.db .venv/bin/python -m uvicorn app.main:app --port 8000
.venv/bin/python scripts/selfcheck.py http://127.0.0.1:8000
.venv/bin/python -m pytest tests/ -q
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/reports` | 实例上报；`201` 新建，`200` 原样重传（`duplicate=true`），`409` 异文拒收，`422` 校验失败 |
| POST | `/api/v1/publish` | 按 `deadline` 发布不可变快照；`201` |
| GET | `/api/v1/published/{publish_id}` | 按发布编号回查（旧批次不受迟到数据影响） |
| GET | `/api/v1/published` | 列出发布批次（倒序，`limit`） |
| GET | `/api/v1/reports` | 查询已收上报（`service` / `source_batch` / `limit`） |
| GET | `/healthz` | 存活探针 |

### 上报示例

```bash
curl -s -X POST localhost:8000/api/v1/reports -H 'content-type: application/json' -d '{
  "service": "checkout-api",
  "instance": "checkout-1",
  "window_start": "2026-11-22T00:00:00Z",
  "window_end":   "2026-11-22T00:05:00Z",
  "source_batch": "batch-a",
  "bucket_boundaries": [100000, 200000, 300000, 500000],
  "bucket_counts": [12, 30, 20, 8, 3],
  "total_count": 73,
  "total_sum_us": 14250000,
  "collected_at": "2026-11-22T00:06:10Z"
}'
```

`bucket_counts` 长度必须为 `len(bucket_boundaries) + 1`，末桶为 `(最后边界, +inf)`；
`total_count` 必须等于各桶之和，否则 `422`——**缺桶不会被当零补上**。

### 发布示例

```bash
curl -s -X POST localhost:8000/api/v1/publish -H 'content-type: application/json' -d '{
  "service": "checkout-api",
  "window_start": "2026-11-22T00:00:00Z",
  "window_end":   "2026-11-22T00:05:00Z",
  "deadline":     "2026-11-22T00:07:00Z",
  "p95_target_us": 250000,
  "expected_instances": ["checkout-1", "checkout-2"]
}'
```

响应要点：

- `p50` / `p95` / `p99`：`{lower_us, upper_us, bucket_label, rank, ...}`，
  真实分位数保证落在闭区间内；`upper_us: null` 表示落入无上界溢出桶。
- `merged_bucket_count_ranges`：按共同边界的合并计数区间（`[lower, upper]`）。
  不同实例边界不对齐时桶无法精确切分，这里如实给出区间而不是编造精确值。
- `missing_instances`：截止前未上报的应到实例，非空则整体 `status=pending_review`。
- `excluded_late_instances` / `unexpected_instances`：分别列出截止后才到、和不在应到清单内的实例。
- `status` ∈ `meets` / `misses` / `pending_review`，附中文 `status_label` 与 `p95_evaluation.reason`。

## 关键实现决定

1. **不做"边界并集后直接分桶"。** 若 A 上报 `(100,500]`、B 在 `200` 有边界，并集边界会把 A 的桶劈开。
   实现改为在所有候选边界上计算合并 CDF 的上下界（`cum_lower` / `cum_upper`），
   输出**保证包含真值**的闭区间，误差只来自真实的信息缺失。`tests/test_aggregate.py`
   用随机枚举一致数据做过属性测试。
2. **幂等指纹**覆盖全部业务字段（含批号、采集时间、实例自报的 p95 目标），
   数字规范化（`1` 与 `1.0` 相同），字段顺序无关；服务端字段不参与。
   同键异文返回 `409`，原记录保持不变。
3. **发布是单写事务。** `publish_id` 分配、上报行读取、结果快照写入都在同一个
   `BEGIN IMMEDIATE` 事务里，避免并发上报导致读到半套数据。
4. **时间统一 UTC 微秒精度**，`+08:00` 与 `Z` 表示同一时刻时命中同一幂等键。
5. SQLite WAL + `synchronous=FULL`，进程内单连接加锁；发布序号用 `seq` 表而非
   `AUTOINCREMENT`，保证不可变快照的编号单调可回查。

## 已验证

- `pytest`：**54 项通过**，覆盖
  - 直方图校验（长度、递增、非负、计数自洽、`count=0` 时 `sum=0`）；
  - 分位数闭区间正确性（含 2000 轮随机一致数据属性测试）；
  - 达标 / 未达标 / 目标落在区间内 / 溢出桶 / 未提供目标；
  - 幂等重传（含数字格式与字段顺序差异）、异文 `409` 且原记录不变；
  - 8 线程并发同报文只插入一条；6 线程并发发布编号唯一且连续；
  - 发布快照与并发写入的原子性（`total_count == 10 * instance_count` 恒成立）；
  - 截止时刻过滤、迟到实例进入新批次、旧 `publish_id` 回查内容不变；
  - 重启后数据与幂等键仍有效。
- 真实 `uvicorn` 进程 + `scripts/selfcheck.py`：**16 步全部 PASS**（见下）。
- OpenAPI 3.1 文档可生成，`/docs` 与 `/openapi.json` 均 `200`。
- `pyflakes` 无告警。

自测输出（节选）：

```
[PASS] 原样重传只收一次（200 + duplicate=true）
[PASS] 异文重传拒收（409）
[PASS] 不完整直方图拒收（422，缺桶不当零）
[PASS] 首轮发布：缺实例列为待核
[PASS] 缺失实例未按零计入
[PASS] 次轮发布：新批次收齐两实例
[PASS] 旧统计按编号回查且保持不变
自测全部通过。
```

## 未完成 / 已知限制

- **未在本机实际构建 Docker 镜像**：当前环境没有 `docker`/`podman`。
  `Dockerfile` 与 `docker-compose.yml` 已写就并对 YAML 做了结构校验，
  但 `docker compose up` 未经真实执行验证；请在具备 Docker 的机器上先跑
  `docker compose up --build -d && docker compose run --rm selfcheck`。
- **单实例部署**：SQLite + 进程内锁适合单副本。多副本需要换成 Postgres/分布式锁，
  并重新审视 `publish_id` 分配。
- **无鉴权与限流**：接口假定在内网或由网关保护。
- **无窗口自动收尾**：截止时刻由发布方显式传入，没有后台定时发布。
- **分位数是"桶内均匀位置未知"的口径**：不假设桶内线性插值，因此区间可能较宽；
  这是刻意的保守选择。
- 未实现上报数据的保留期清理（`GET /api/v1/reports` 只做倒序 `limit`）。
