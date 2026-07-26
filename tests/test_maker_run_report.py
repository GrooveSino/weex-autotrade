from datetime import UTC, datetime

from weex_cli.reporting import build_maker_run_report, build_maker_soak_report


def test_report_summarizes_run_without_order_identifiers() -> None:
    payload = {
        "status": "completed",
        "reason": "target_reached",
        "plan": {"symbol": "BTCSUSDT", "target_quote_volume": "10000", "fills": 2},
        "total_quote_volume": "10020.5",
        "maker_only": True,
        "final_position": 0,
        "active_order_count": 0,
        "elapsed_seconds": 100.5,
        "legs": [
            {
                "sequence": 1,
                "action": "open",
                "status": "completed",
                "reason": "target_reached",
                "elapsed_ms": 50_000,
                "submissions": 1,
                "cancels": 0,
                "venue_cancels": 0,
                "post_only_rejections": 0,
                "fill_count": 1,
                "quote_volume": 5010.25,
                "final_position": 0.08,
                "events": [
                    {"event": "submit", "order_id": "secret-order-id"},
                    {"event": "fill", "order_id": "secret-order-id", "maker": True},
                ],
            },
            {
                "sequence": 2,
                "action": "close",
                "status": "completed",
                "reason": "target_reached",
                "elapsed_ms": 50_500,
                "submissions": 1,
                "cancels": 0,
                "venue_cancels": 0,
                "post_only_rejections": 0,
                "fill_count": 1,
                "quote_volume": 5010.25,
                "final_position": 0,
                "events": [],
            },
        ],
    }

    report = build_maker_run_report(
        payload,
        baseline_seconds=427.008,
        generated_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert "## 技术结论" in report
    assert "## 逐腿执行日志" in report
    assert "326.508 s 更快" in report
    assert "secret-order-id" not in report
    assert "Post-Only 拒绝为 0 | 通过" in report


def test_report_surfaces_post_only_rejection_as_failure() -> None:
    report = build_maker_run_report(
        {
            "status": "failed",
            "reason": "post_only_rejected",
            "plan": {"symbol": "BTCSUSDT", "target_quote": "10000", "fills": 10},
            "total_quote_volume": "0",
            "maker_only": True,
            "final_position": 0,
            "active_order_count": 0,
            "elapsed_seconds": 1,
            "legs": [
                {
                    "sequence": 1,
                    "action": "open",
                    "status": "failed",
                    "reason": "post_only_rejected",
                    "post_only_rejections": 1,
                    "venue_cancels": 1,
                    "events": [{"event": "post_only_rejection", "reason": "COULD_NOT_FILL"}],
                }
            ],
        }
    )

    assert "`COULD_NOT_FILL` × 1" in report
    assert "Post-Only 拒绝为 0 | 失败" in report


def test_report_does_not_treat_unknown_final_state_as_flat() -> None:
    report = build_maker_run_report(
        {
            "status": "failed",
            "reason": "deadline_exceeded",
            "plan": {"symbol": "BTCSUSDT", "target_quote": "10000", "fills": 10},
            "total_quote_volume": "1000",
            "maker_only": True,
            "final_position": None,
            "active_order_count": None,
            "elapsed_seconds": 120,
            "legs": [],
        }
    )

    assert "最终仓位为 0 | 失败" in report
    assert "最终活动订单为 0 | 失败" in report


def test_soak_report_summarizes_rounds_without_order_ids() -> None:
    payload = {
        "status": "completed",
        "reason": "all_rounds_completed",
        "rounds_requested": 2,
        "rounds_completed": 2,
        "total_quote_volume": "20200",
        "elapsed_seconds": 200,
        "total_post_only_rejections": 0,
        "rounds": [
            {
                "round": index,
                "status": "completed",
                "reason": "target_reached",
                "total_quote_volume": "10100",
                "elapsed_seconds": 95,
                "submission_count": 10,
                "final_position": 0,
                "active_order_count": 0,
                "legs": [
                    {
                        "sequence": 1,
                        "action": "open",
                        "status": "completed",
                        "elapsed_ms": 1000,
                        "submissions": 1,
                        "fill_count": 1,
                        "quote_volume": 1010,
                        "final_position": 0.016,
                        "events": [{"order_id": "must-not-appear"}],
                    }
                ],
            }
            for index in (1, 2)
        ],
    }

    report = build_maker_soak_report(payload)

    assert "完成 2/2 轮" in report
    assert "每轮最终空仓 | 通过" in report
    assert "| 1 | completed | target_reached | 10100 | 95.000 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |" in report
    assert "must-not-appear" not in report
