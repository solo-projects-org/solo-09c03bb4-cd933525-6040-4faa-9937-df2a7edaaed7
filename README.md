# 延迟 SLO 评审 API（FastAPI + SQLite）

接收各实例上报的延迟直方图，按 UTC 窗口和截止时刻合并发布 p50/p95/p99 的**必含误差区间**，
并对 p95 目标给出**达标 / 未达标 / 待核**三态结论。p95 目标由性能数据员**按服务登记为
带版本号与 UTC 生效时刻的不可变版本**，发布时自动选取窗口结束前最后生效的一版。
业务口径见 [`SPEC.md`](SPEC.md)。

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
curl -s http://127.0.0.1:8000/healthz
```

- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI JSON：<http://127.0.0.1:8000/openapi.json>
- 数据持久化在具名卷 `slo-data`（容器内 `/data/slo.db`）。

跑一遍端到端自测（含目标登记/冲突、幂等、缺实例、迟到批次、登记与发布交错、旧批号回查）：

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

## Swagger 演示：登记目标 → 发布 → 回查

打开 <http://127.0.0.1:8000/docs>，按顺序执行：

1. **`POST /api/v1/p95-targets`** 登记目标版本（性能数据员）：
   ```json
   {
     "service": "checkout-api",
     "version": "p95-2026-11-22-r1",
     "p95_target_us": 250000,
     "effective_at": "2026-11-22T00:00:00Z"
   }
   ```
   再次原样提交返回 `200` 且 `duplicate=true`；把 `p95_target_us` 改掉再提交同版本号 → `409`，
   原登记保持不变（版本不可修改，调整需换版本号）。
2. **`POST /api/v1/reports`** 上报各实例直方图（示例见下）。
3. **`POST /api/v1/publish`** 发起发布评审——请求体**不含** p95 目标，系统自动选取
   `effective_at <= window_end` 的最后生效版本；没有符合版本时返回 `409` 且不生成发布记录。
4. **`GET /api/v1/published/{publish_id}`** 按发布号回查：响应中的 `p95_target_version`、
   `p95_target` 与 `p95_evaluation` 即当时固化的目标版本与达标结论。
5. 登记一版生效时刻更晚的新目标后再次 `POST /api/v1/publish`，新发布采用新版本，
   旧发布号回查仍指向旧版本（登记与发布交错的典型场景）。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/p95-targets` | 登记 p95 目标版本；`201` 新登记，`200` 同版本原样重传（返回原版本），`409` 同版本内容有变（不可修改），`422` 校验失败 |
| GET | `/api/v1/p95-targets` | 查询已登记目标版本（`service` 过滤、`limit`，倒序） |
| POST | `/api/v1/reports` | 实例上报；`201` 新建，`200` 原样重传（`duplicate=true`），`409` 异文拒收，`422` 校验失败 |
| POST | `/api/v1/publish` | 按 `deadline` 发布不可变快照；目标版本自动选取；`409` 无符合条件的目标版本（不生成发布记录） |
| GET | `/api/v1/published/{publish_id}` | 按发布编号回查（含当时的目标版本号、目标值与达标结论；旧批次不受迟到数据影响） |
| GET | `/api/v1/published` | 列出发布批次（倒序，`limit`，含 `p95_target_version`） |
| GET | `/api/v1/reports` | 查询已收上报（`service` / `source_batch` / `limit`） |
| GET | `/healthz` | 存活探针 |

### p95 目标登记示例

```bash
curl -s -X POST localhost:8000/api/v1/p95-targets -H 'content-type: application/json' -d '{
  "service": "checkout-api",
  "version": "p95-2026-11-22-r1",
  "p95_target_us": 250000,
  "effective_at": "2026-11-22T00:00:00Z"
}'
```

- 键为 `(service, version)`；指纹覆盖目标值与生效时刻，数字 `250000` 与 `250000.0` 等价、字段顺序无关。
- 发布选取口径：`effective_at <= window_end` 的已登记版本中，生效时刻最新者；
  同一生效时刻取最近登记的一版。窗口结束后才生效的版本不参与本次发布。

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
  "expected_instances": ["checkout-1", "checkout-2"]
}'
```

请求体不再接受 `p95_target_us`（携带会被 `422` 拒收）。若该服务在窗口结束（含）前
没有任何已登记且生效的目标版本，返回 `409 no_eligible_p95_target_version`，
**不生成发布记录、不占用发布编号**。

响应要点：

- `p95_target_version` / `p95_target_us` / `p95_target`：本次发布实际采用的目标版本号、
  目标值与版本快照（生效时刻、登记时刻、选用口径）。
- `p50` / `p95` / `p99`：`{lower_us, upper_us, bucket_label, rank, ...}`，
  真实分位数保证落在闭区间内；`upper_us: null` 表示落入无上界溢出桶。
- `merged_bucket_count_ranges`：按共同边界的合并计数区间（`[lower, upper]`）。
  不同实例边界不对齐时桶无法精确切分，这里如实给出区间而不是编造精确值。
- `missing_instances`：截止前未上报的应到实例，非空则整体 `status=pending_review`。
- `excluded_late_instances` / `unexpected_instances`：分别列出截止后才到、和不在应到清单内的实例。
- `status` ∈ `meets` / `misses` / `pending_review`，附中文 `status_label` 与 `p95_evaluation.reason`；
  这些结论与目标版本号一起固化在快照中，之后只能按 `publish_id` 回查。

## 关键实现决定

1. **不做"边界并集后直接分桶"。** 若 A 上报 `(100,500]`、B 在 `200` 有边界，并集边界会把 A 的桶劈开。
   实现改为在所有候选边界上计算合并 CDF 的上下界（`cum_lower` / `cum_upper`），
   输出**保证包含真值**的闭区间，误差只来自真实的信息缺失。`tests/test_aggregate.py`
   用随机枚举一致数据做过属性测试。
2. **幂等指纹**覆盖全部业务字段（含批号、采集时间、实例自报的 p95 目标），
   数字规范化（`1` 与 `1.0` 相同），字段顺序无关；服务端字段不参与。
   同键异文返回 `409`，原记录保持不变。
3. **p95 目标版本不可变。** 新表 `p95_targets` 以 `(service, version)` 为唯一键，
   指纹覆盖目标值与 UTC 生效时刻：原样重传 `200` 返回原版本，异文 `409` 拒收，
   不提供修改/删除接口。发布在写事务内按 `effective_at <= window_end`
   选最后生效版本（同时刻取最近登记），选不到就抛错回滚——**拒绝发布不占用 `publish_id`、
   不写 `publishes`**。
4. **目标与结论随发布快照固化。** 发布结果内嵌 `p95_target_version`、`p95_target`
   与含 `target_us` 的 `p95_evaluation`；后续登记新版本不影响历史发布号的回查结果。
5. **发布是单写事务。** `publish_id` 分配、目标版本选取、上报行读取、结果快照写入都在同一个
   `BEGIN IMMEDIATE` 事务里，避免并发上报/登记导致读到半套数据。
6. **时间统一 UTC 微秒精度**，`+08:00` 与 `Z` 表示同一时刻时命中同一幂等键。
7. SQLite WAL + `synchronous=FULL`，进程内单连接加锁；发布序号用 `seq` 表而非
   `AUTOINCREMENT`，保证不可变快照的编号单调可回查。

## 已验证

- `pytest`：**71 项通过**（基线 54 项 + 新增 17 项目标版本相关用例），覆盖
  - 直方图校验（长度、递增、非负、计数自洽、`count=0` 时 `sum=0`）；
  - 分位数闭区间正确性（含 2000 轮随机一致数据属性测试）；
  - 达标 / 未达标 / 目标落在区间内 / 溢出桶；
  - 幂等重传（含数字格式与字段顺序差异）、异文 `409` 且原记录不变；
  - 8 线程并发同报文只插入一条；6 线程并发发布编号唯一且连续；
  - 发布快照与并发写入的原子性（`total_count == 10 * instance_count` 恒成立）；
  - 截止时刻过滤、迟到实例进入新批次、旧 `publish_id` 回查内容不变；
  - 重启后数据、幂等键与目标登记仍有效；
  - **目标版本登记**：新建/原样重传返回原版本/异文 `409` 不可变、时区归一化、服务间隔离；
  - **发布选版**：取窗口结束前最后生效版本、恰在窗口结束时刻可选用、之后生效不选用、
    同生效时刻取最近登记、无符合版本 `409` 拒绝且不占用编号；
  - **登记与发布交错**：首轮发布固化 v1，随后登记 v2，次轮发布自动采用 v2，
    旧发布号回查仍为 v1 的目标与结论。
- 真实 `uvicorn` 进程 + `scripts/selfcheck.py`：**24 步全部 PASS**（见下）。
- OpenAPI 3.1 文档可生成，`/docs` 与 `/openapi.json` 均 `200`，新接口带 `p95-targets` 分组与示例。
- `pyflakes` 无告警。

自测输出（节选）：

```
[PASS] 登记 p95 目标版本 v1（201）
[PASS] 同版本原样重传返回原版本（200 + duplicate=true）
[PASS] 同版本内容有变报冲突（409），原版本不可修改
[PASS] 无符合条件的目标版本时拒绝发布（409，不生成发布记录）
[PASS] 发布请求不再接受 p95 目标入参（422）
[PASS] 首轮发布：缺实例列为待核，采用已登记目标 v1
[PASS] 次轮发布：新批次收齐两实例，自动选用最后生效的 v2
[PASS] 窗口结束后才生效的版本不被选用（仍选 v2）
[PASS] 旧发布按编号回查且目标版本与统计均保持不变
自测全部通过。
```

## 未完成 / 已知限制

- **未在本机实际构建 Docker 镜像**：当前环境没有 `docker`/`podman`（命令不存在）。
  `Dockerfile` 与 `docker-compose.yml` 保持可用且本次未改变其结构，
  但 `docker compose up` 未经真实执行验证；已在本机用真实 `uvicorn` 进程完成等价验证
  （selfcheck 24 步全过）。请在具备 Docker 的机器上跑
  `docker compose up --build -d && docker compose run --rm selfcheck`。
- **单实例部署**：SQLite + 进程内锁适合单副本。多副本需要换成 Postgres/分布式锁，
  并重新审视 `publish_id` 分配。
- **无鉴权与限流**：接口假定在内网或由网关保护。
- **无窗口自动收尾**：截止时刻由发布方显式传入，没有后台定时发布。
- **分位数是"桶内均匀位置未知"的口径**：不假设桶内线性插值，因此区间可能较宽；
  这是刻意的保守选择。
- 未实现上报数据与目标版本的保留期清理（列表接口只做倒序 `limit`）。
- 实例上报中的 `p95_target_us` 字段仍保留为"仅记录"的观测信息，不参与发布判定；
  发布判定只以登记的目标版本为准。
