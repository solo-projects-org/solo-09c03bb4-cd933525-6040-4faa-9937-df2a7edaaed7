#!/usr/bin/env python3
"""End-to-end self-check against a running API (Docker Compose or local).

Exercises the critical flow and prints a PASS/FAIL line per step:

  1. health + OpenAPI
  2. p95 target version registration (immutable, per service)
  3. identical target re-send returns the original version (200, duplicate=true)
  4. same version with changed content is rejected (409), original untouched
  5. ingest two instances with different bucket boundaries
  6. identical re-send is deduplicated (200, duplicate=true)
  7. conflicting re-send is rejected (409) and the original is untouched
  8. incomplete histogram (missing bucket) is rejected (422)
  9. publish without an eligible target version is rejected (409), no record
 10. publish no longer accepts a p95_target_us parameter (422)
 11. publish before the late instance arrives -> missing instance, 待核
 12. late instance arrives, new batch publishes 达标/未达标/待核
 13. interleaved: a newly registered target version is picked by the next publish
 14. old snapshot is still retrievable unchanged by publish id (target version kept)

Usage:  python scripts/selfcheck.py [base_url]
Env:    SLO_API_BASE (default http://127.0.0.1:8000)
Exit code 0 only if every step passes.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SLO_API_BASE", "http://127.0.0.1:8000")).rstrip("/")
SERVICE = "selfcheck-api"
FAILURES: list[str] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    window_start = now - timedelta(minutes=5)
    window_end = now
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    batch = f"selfcheck-{stamp}"

    with httpx.Client(base_url=BASE, timeout=10.0) as client:
        response = client.get("/healthz")
        step("健康检查 /healthz", response.status_code == 200, response.text[:120])

        spec = client.get("/openapi.json")
        step(
            "OpenAPI / Swagger 可用",
            spec.status_code == 200
            and "/api/v1/publish" in spec.text
            and "/api/v1/p95-targets" in spec.text,
            "/docs",
        )

        # ---- p95 target registration -------------------------------------------------
        target_v1 = {
            "service": SERVICE,
            "version": f"target-{stamp}-v1",
            "p95_target_us": 250000,
            "effective_at": iso(window_start),
        }
        r = client.post("/api/v1/p95-targets", json=target_v1)
        target_row = r.json() if r.status_code == 201 else {}
        step(
            "登记 p95 目标版本 v1（201）",
            r.status_code == 201 and target_row.get("duplicate") is False,
            r.text[:160],
        )

        r = client.post("/api/v1/p95-targets", json=target_v1)
        step(
            "同版本原样重传返回原版本（200 + duplicate=true）",
            r.status_code == 200
            and r.json().get("duplicate") is True
            and r.json().get("target_id") == target_row.get("target_id"),
            r.text[:160],
        )

        conflicting_target = dict(target_v1, p95_target_us=200000)
        r = client.post("/api/v1/p95-targets", json=conflicting_target)
        step(
            "同版本内容有变报冲突（409），原版本不可修改",
            r.status_code == 409 and r.json().get("error") == "conflicting_p95_target_version",
            r.text[:160],
        )

        listed_targets = client.get("/api/v1/p95-targets", params={"service": SERVICE})
        step(
            "冲突未覆盖原登记目标",
            listed_targets.status_code == 200
            and any(row["version"] == target_v1["version"] and row["p95_target_us"] == 250000
                    for row in listed_targets.json()),
            f"versions={[row['version'] for row in listed_targets.json()][:5]}",
        )

        # A second service with no target at all: its first publish must be rejected.
        other_service = f"{SERVICE}-no-target"

        # ---- reports -----------------------------------------------------------------
        def report(instance: str, boundaries, counts, total, total_sum):
            return {
                "service": SERVICE,
                "instance": instance,
                "window_start": iso(window_start),
                "window_end": iso(window_end),
                "p95_target_us": 250000,
                "source_batch": batch,
                "bucket_boundaries": boundaries,
                "bucket_counts": counts,
                "total_count": total,
                "total_sum_us": total_sum,
                "collected_at": iso(now + timedelta(seconds=1)),
            }

        body_a = report("inst-a", [100000, 200000, 300000], [10, 20, 0, 0], 30, 4_000_000)
        body_b = report("inst-b", [150000, 400000], [12, 18, 0], 30, 7_500_000)

        r = client.post("/api/v1/reports", json=body_a)
        step("接收实例 A（自定义桶边界）", r.status_code == 201, r.text[:160])

        r = client.post("/api/v1/reports", json=body_a)
        step(
            "原样重传只收一次（200 + duplicate=true）",
            r.status_code == 200 and r.json().get("duplicate") is True,
            r.text[:160],
        )

        conflicting = dict(body_a, total_sum_us=9_999_999)
        r = client.post("/api/v1/reports", json=conflicting)
        step(
            "异文重传拒收（409）",
            r.status_code == 409 and r.json().get("error") == "conflicting_report",
            r.text[:160],
        )

        listed = client.get("/api/v1/reports", params={"service": SERVICE, "source_batch": batch})
        step(
            "冲突未覆盖原记录",
            listed.status_code == 200 and listed.json()[0]["total_sum_us"] == 4_000_000,
            f"records={len(listed.json())}",
        )

        broken = report("inst-x", [100000, 200000], [1, 2, 0], 10, 1000)
        r = client.post("/api/v1/reports", json=broken)
        step("不完整直方图拒收（422，缺桶不当零）", r.status_code == 422, r.text[:160])

        publish_body = {
            "service": SERVICE,
            "window_start": iso(window_start),
            "window_end": iso(window_end),
            "expected_instances": ["inst-a", "inst-b"],
            "deadline": iso(now + timedelta(seconds=2)),
        }

        # No eligible target version for this service -> reject, no publish record.
        no_target_body = dict(publish_body, service=other_service)
        r = client.post("/api/v1/publish", json=no_target_body)
        step(
            "无符合条件的目标版本时拒绝发布（409，不生成发布记录）",
            r.status_code == 409 and r.json().get("error") == "no_eligible_p95_target_version",
            r.text[:160],
        )
        history = client.get("/api/v1/published").json()
        step(
            "被拒发布未占用发布编号",
            all(row["service"] != other_service for row in history),
            f"publish_ids={[row['publish_id'] for row in history][:5]}",
        )

        r = client.post(
            "/api/v1/publish",
            json=dict(publish_body, p95_target_us=250000),
        )
        step(
            "发布请求不再接受 p95 目标入参（422）",
            r.status_code == 422,
            r.text[:160],
        )

        r = client.post("/api/v1/publish", json=publish_body)
        first = r.json() if r.status_code == 201 else {}
        step(
            "首轮发布：缺实例列为待核，采用已登记目标 v1",
            r.status_code == 201
            and first.get("missing_instances") == ["inst-b"]
            and first.get("status") == "pending_review"
            and first.get("p95_target_version") == target_v1["version"]
            and first.get("p95_target_us") == 250000,
            f"status={first.get('status')} target={first.get('p95_target_version')}",
        )
        step(
            "缺失实例未按零计入",
            first.get("total_count") == 30 and first.get("instance_count") == 1,
            f"total_count={first.get('total_count')} instance_count={first.get('instance_count')}",
        )
        step(
            "p50/p95/p99 均给出必含区间",
            all(first.get(key) is not None for key in ("p50", "p95", "p99")),
            f"p95={first.get('p95')}",
        )

        r = client.post("/api/v1/reports", json=body_b)
        step("迟到实例 B 进入新批次接收", r.status_code == 201, r.text[:160])

        # Interleaved registration: a newer target version in force before the window ends.
        target_v2 = {
            "service": SERVICE,
            "version": f"target-{stamp}-v2",
            "p95_target_us": 200000,
            "effective_at": iso(window_end - timedelta(seconds=30)),
        }
        r = client.post("/api/v1/p95-targets", json=target_v2)
        step(
            "登记更新的目标版本 v2（201）",
            r.status_code == 201 and r.json().get("version") == target_v2["version"],
            r.text[:160],
        )

        second_body = dict(publish_body, deadline=iso(now + timedelta(seconds=5)))
        r = client.post("/api/v1/publish", json=second_body)
        second = r.json() if r.status_code == 201 else {}
        step(
            "次轮发布：新批次收齐两实例，自动选用最后生效的 v2",
            r.status_code == 201
            and second.get("missing_instances") == []
            and second.get("instance_count") == 2
            and second.get("p95_target_version") == target_v2["version"]
            and second.get("p95_target_us") == 200000
            and second.get("publish_id") == first.get("publish_id", 0) + 1,
            f"publish_id={second.get('publish_id')} target={second.get('p95_target_version')} "
            f"status={second.get('status')}",
        )
        step(
            "合并后共同边界为两实例边界并集",
            second.get("common_bucket_boundaries") == [100000, 150000, 200000, 300000, 400000],
            f"boundaries={second.get('common_bucket_boundaries')}",
        )
        step(
            "p95 目标给出三态之一且结论随版本发布",
            second.get("status") in {"meets", "misses", "pending_review"}
            and second.get("p95_evaluation", {}).get("target_us") == 200000,
            f"status={second.get('status')} label={second.get('status_label')} "
            f"reason={second.get('p95_evaluation', {}).get('reason')}",
        )

        # A version effective only after the window end must never be selected.
        future_target = dict(
            target_v2,
            version=f"target-{stamp}-future",
            effective_at=iso(window_end + timedelta(minutes=10)),
        )
        client.post("/api/v1/p95-targets", json=future_target)
        third = client.post(
            "/api/v1/publish",
            json=dict(second_body, deadline=iso(now + timedelta(seconds=8))),
        ).json()
        step(
            "窗口结束后才生效的版本不被选用（仍选 v2）",
            third.get("p95_target_version") == target_v2["version"],
            f"selected={third.get('p95_target_version')}",
        )

        old = client.get(f"/api/v1/published/{first.get('publish_id')}")
        step(
            "旧发布按编号回查且目标版本与统计均保持不变",
            old.status_code == 200
            and old.json().get("missing_instances") == ["inst-b"]
            and old.json().get("total_count") == 30
            and old.json().get("p95_target_version") == target_v1["version"],
            old.text[:200],
        )

        history = client.get("/api/v1/published", params={"limit": 5})
        step(
            "发布历史倒序可查，列表携带目标版本号",
            history.status_code == 200
            and history.json()[0]["publish_id"] == third.get("publish_id")
            and history.json()[0]["p95_target_version"] == target_v2["version"],
            f"ids={[row['publish_id'] for row in history.json()]}",
        )

    print()
    if FAILURES:
        print(f"自测失败 {len(FAILURES)} 项：" + "; ".join(FAILURES))
        return 1
    print("自测全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
