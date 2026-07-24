from decimal import Decimal

from weex_cli.execution_progress import ExecutionProgressProjector, describe_execution_event


def leg_wait(symbol: str, sequence: int) -> dict[str, object]:
    return {
        "event": "leg_progress",
        "progress_event": "wait",
        "waiting_for": "maker_fill",
        "round": 1,
        "sequence": sequence,
        "symbol": symbol,
        "action": "open",
        "elapsed_ms": 1_000,
        "remaining_ms": 9_000,
        "filled_quantity": "0.001",
        "order_quantity": "0.002",
    }


def test_projector_keeps_parallel_legs_and_clears_them_at_pair_barrier() -> None:
    projector = ExecutionProgressProjector()

    assert projector.apply(leg_wait("BTC", 1), at_ms=1_000) is None
    assert projector.apply(leg_wait("ETH", 2), at_ms=1_010) is None
    assert {wait.symbol for wait in projector.active_waits.values()} == {"BTC", "ETH"}

    presentation = projector.apply(
        {"event": "pair_wait_completed", "round": 1, "action": "open"},
        at_ms=2_000,
    )

    assert projector.active_waits == {}
    assert presentation is not None
    assert presentation.title == "BTC/ETH 双腿屏障已通过"


def test_projector_reads_leg_sequence_without_confusing_journal_sequence() -> None:
    projector = ExecutionProgressProjector()
    event = leg_wait("BTC", 41)
    event["sequence"] = 900
    event["fields"] = {**event, "leg_sequence": 41}

    projector.apply(event, at_ms=1_000)

    assert next(iter(projector.active_waits)).startswith("leg:1:41:BTC:open")


def test_wait_heartbeats_never_become_generic_timeline_noise() -> None:
    assert describe_execution_event(leg_wait("BTC", 1)) is None
    assert describe_execution_event({"event": "pair_wait_progress", "round": 1}) is None


def test_known_progress_events_have_human_semantics() -> None:
    presentation = describe_execution_event(
        {
            "event": "leg_progress",
            "progress_event": "submit",
            "symbol": "BTC",
            "action": "open",
            "price": "71000",
            "quantity": "0.001",
        }
    )

    assert presentation is not None
    assert presentation.title == "BTC 开仓 Maker 挂单已提交"
    assert "leg progress" not in presentation.message.lower()


def test_timeline_projection_covers_the_supported_execution_event_catalog() -> None:
    common = {
        "run": 2,
        "round": 3,
        "symbol": "BTC",
        "action": "open",
        "remaining_quote": "400",
        "child_quote": "80",
        "total_quote": "160",
        "reason": "network_retry",
        "attempt": 1,
        "max_attempts": 3,
        "seconds": "2.5",
        "quantity": "0.01",
        "price": "65000",
        "quote_volume": "65",
        "fill_count": 1,
        "desired_quote": "80",
        "btc_quantity": "0.001",
        "eth_quantity": "0.01",
        "leverage": 400,
        "status": "completed",
        "completed": True,
        "flat": True,
        "no_orders": True,
        "maker_only": True,
        "executed_quote_volume": "160",
    }
    cases = (
        ({"event": "campaign_run_started"}, "Campaign 运行 2 开始"),
        ({"event": "campaign_run_completed", "child_plan_id": "plan-2"}, "Campaign 运行 2 已保存检查点"),
        ({"event": "phase_pacing_completed", "phase": "open"}, "全局执行错峰完成"),
        ({"event": "phase_pacing_cancelled"}, "全局执行错峰已取消"),
        ({"event": "campaign_boundary_completed", "phase": "initial"}, "账户边界读取完成"),
        ({"event": "campaign_boundary_completed", "phase": "cycle"}, "账户边界读取完成"),
        ({"event": "campaign_child_planning_completed", "child_plan_id": "plan-2"}, "BTC/ETH 子计划生成完成"),
        ({"event": "campaign_read_retry"}, "Campaign 只读检查失败，等待重试"),
        ({"event": "campaign_finished"}, "Campaign 已完成"),
        ({"event": "campaign_finished", "status": "stopped"}, "Campaign 已停止"),
        ({"event": "preflight_completed"}, "执行前检查完成"),
        ({"event": "preflight_rejected"}, "执行前检查未通过"),
        ({"event": "preflight_retry", "error": "timeout"}, "执行前检查读取失败，等待重试"),
        ({"event": "cycle_started"}, "第 3 轮开始"),
        ({"event": "cycle_leverage_ready"}, "BTC/ETH 杠杆准备完成"),
        ({"event": "cycle_sizing_retry"}, "盘口读取失败，等待重新计算数量"),
        ({"event": "cycle_read_retry"}, "账户参数读取失败，等待重查"),
        ({"event": "leg_started", "side": "buy"}, "BTC 开仓准备完成"),
        ({"event": "leg_completed"}, "BTC 开仓成交已核验"),
        ({"event": "leg_stopped"}, "BTC 开仓已安全停止"),
        ({"event": "leg_uncertain"}, "BTC 开仓状态不确定"),
        ({"event": "position_observation_unavailable"}, "BTC 开仓仓位读取失败"),
        ({"event": "pair_wait_completed"}, "BTC/ETH 双腿屏障已通过"),
        ({"event": "close_barrier_started"}, "开仓阶段结束"),
        ({"event": "accounting_wait_completed"}, "BTC 成交明细对账完成"),
        ({"event": "accounting_retry_wait"}, "BTC 成交明细尚未完整"),
        ({"event": "hold_completed"}, "双边持仓等待完成"),
        ({"event": "round_gap_completed"}, "轮次间隔完成"),
        ({"event": "final_acceptance_completed"}, "最终验收通过"),
        ({"event": "final_acceptance_completed", "completed": False}, "最终验收未通过"),
        ({"event": "cycle_completed"}, "第 3 轮 completed"),
        ({"event": "cycle_stopped", "status": "stopped"}, "第 3 轮 stopped"),
        ({"event": "workflow_finished"}, "执行流程 已完成"),
        ({"event": "workflow_finished", "status": "failed"}, "执行流程 失败"),
        ({"event": "campaign_uncertain"}, "执行结果不确定"),
        ({"event": "safe_stop_started"}, "安全停止已接管"),
        ({"event": "safe_stop_cancel_verified"}, "BTC 撤单已核验"),
        ({"event": "safe_stop_cancel_unverified"}, "BTC 撤单未能核验"),
        ({"event": "safe_stop_flattening"}, "BTC 正在 Maker-only 平仓"),
        ({"event": "safe_stop_leg_completed"}, "BTC Maker-only 平仓已完成"),
        ({"event": "safe_stop_verified"}, "安全停止已核验"),
        ({"event": "safe_stop_uncertain"}, "安全停止结果待核验"),
        ({"event": "campaign_reconciliation_acknowledged"}, "人工对账已记录"),
        ({"event": "leg_progress", "progress_event": "market_data_source", "source": "websocket"}, "BTC 开仓盘口来源"),
        ({"event": "leg_progress", "progress_event": "fill"}, "BTC 开仓观察到 Maker 成交"),
        ({"event": "leg_progress", "progress_event": "cancel_started"}, "BTC 开仓报价需要更新"),
        ({"event": "leg_progress", "progress_event": "cancel"}, "BTC 开仓撤单已确认"),
        ({"event": "leg_progress", "progress_event": "preflight_skip"}, "BTC 开仓本地报价检查未通过"),
        ({"event": "leg_progress", "progress_event": "order_terminal"}, "BTC 开仓挂单已进入终态"),
        ({"event": "leg_progress", "progress_event": "timeout_cleanup_started"}, "BTC 开仓达到腿超时"),
        ({"event": "leg_progress", "progress_event": "timeout_cleanup_confirmed"}, "BTC 开仓超时清理已确认"),
        ({"event": "leg_progress", "progress_event": "timeout_cleanup_error"}, "BTC 开仓超时状态未能确认"),
    )

    for fields, expected_title in cases:
        presentation = describe_execution_event({**common, **fields})
        assert presentation is not None
        assert presentation.title == expected_title


def test_projector_aggregates_verified_leg_volume_without_double_counting() -> None:
    projector = ExecutionProgressProjector()
    projector.apply({"event": "campaign_run_started", "run": 1}, at_ms=1_000)
    btc = {
        "event": "leg_completed",
        "run": 1,
        "round": 1,
        "sequence": 1,
        "symbol": "BTCUSDT",
        "action": "open",
        "quote_volume": "33.0978",
    }
    eth = {
        "event": "leg_completed",
        "run": 1,
        "round": 1,
        "sequence": 2,
        "symbol": "ETHUSDT",
        "action": "open",
        "quote_volume": "11.69364",
    }

    projector.apply(btc, at_ms=2_000)
    projector.apply(btc, at_ms=2_001)
    projector.apply(eth, at_ms=2_002)

    snapshot = projector.snapshot()
    assert snapshot["execution_verified_quote_volume"] == "44.79144"
    assert snapshot["btc_quote_volume"] == "33.0978"
    assert snapshot["eth_quote_volume"] == "11.69364"


def test_projector_carries_child_cycle_totals_across_campaign_runs() -> None:
    projector = ExecutionProgressProjector()
    projector.apply({"event": "campaign_run_started", "run": 1}, at_ms=1_000)
    projector.apply(
        {"event": "cycle_completed", "run": 1, "round": 1, "status": "completed", "total_quote": "80.86"},
        at_ms=2_000,
    )
    assert projector.snapshot()["execution_verified_quote_volume"] == "80.86"
    projector.apply({"event": "campaign_run_completed", "run": 1, "total_quote": "80.86"}, at_ms=3_000)
    projector.apply({"event": "campaign_run_started", "run": 2}, at_ms=4_000)
    projector.apply(
        {"event": "cycle_completed", "run": 2, "round": 1, "status": "completed", "total_quote": "89.5868"},
        at_ms=5_000,
    )

    assert projector.snapshot()["execution_verified_quote_volume"] == "170.4468"
    projector.apply({"event": "campaign_run_completed", "run": 2, "total_quote": "170.4468"}, at_ms=6_000)
    assert projector.snapshot()["execution_verified_quote_volume"] == "170.4468"


def test_projector_exposes_and_clears_hold_and_round_gap_countdowns() -> None:
    projector = ExecutionProgressProjector()

    assert projector.apply({"event": "hold_started", "round": 1, "seconds": "43.6"}, at_ms=1_000) is None
    hold = projector.active_waits["hold"]
    assert hold.label == "双边持仓计时"
    assert hold.remaining_ms == 43_600
    projector.apply({"event": "hold_completed", "round": 1, "seconds": "43.6"}, at_ms=44_600)
    assert "hold" not in projector.active_waits

    assert projector.apply({"event": "round_gap_started", "round": 1, "seconds": "77.6"}, at_ms=45_000) is None
    gap = projector.active_waits["round-gap"]
    assert gap.label == "等待进入下一周期"
    assert gap.remaining_ms == 77_600
    projector.apply({"event": "round_gap_completed", "round": 1, "seconds": "77.6"}, at_ms=122_600)
    assert projector.active_waits == {}


def test_hold_countdown_has_a_visible_target_position_barrier() -> None:
    verified = describe_execution_event({"event": "open_barrier_verified", "round": 1})
    not_ready = describe_execution_event({"event": "open_barrier_not_ready", "round": 1})

    assert verified is not None
    assert verified.title == "BTC/ETH 目标仓位已核验"
    assert not_ready is not None
    assert not_ready.title == "BTC/ETH 目标仓位未达成"


def test_projector_snapshot_restores_dedupe_counts_and_absolute_wait_deadline() -> None:
    projector = ExecutionProgressProjector()
    projector.apply({"event": "campaign_run_started", "run": 1}, at_ms=1_000)
    projector.apply(
        {
            "event": "leg_completed",
            "run": 1,
            "round": 1,
            "sequence": 1,
            "symbol": "BTCUSDT",
            "action": "open",
            "quote_volume": "33.10",
        },
        at_ms=2_000,
    )
    projector.apply({"event": "round_gap_started", "round": 1, "seconds": "10"}, at_ms=3_000)

    restored = ExecutionProgressProjector.from_snapshot(projector.snapshot())
    restored.apply(
        {
            "event": "leg_completed",
            "run": 1,
            "round": 1,
            "sequence": 1,
            "symbol": "BTCUSDT",
            "action": "open",
            "quote_volume": "33.10",
        },
        at_ms=4_000,
    )

    assert restored.snapshot()["btc_quote_volume"] == "33.10"
    assert restored.snapshot()["execution_verified_quote_volume"] == "33.10"
    wait = restored.active_waits["round-gap"]
    assert wait.started_at_ms == 3_000
    assert wait.deadline_at_ms == 13_000


def test_phase_pacing_wait_uses_absolute_deadline_and_clears_on_completion() -> None:
    projector = ExecutionProgressProjector()
    assert (
        projector.apply(
            {
                "event": "phase_pacing_started",
                "phase": "open",
                "round": 2,
                "deadline_at_ms": 21_000,
            },
            at_ms=10_000,
        )
        is None
    )
    wait = projector.active_waits["phase-pacing:2:open"]
    assert wait.label == "全局执行错峰 · 开仓"
    assert wait.deadline_at_ms == 21_000
    completed = projector.apply(
        {"event": "phase_pacing_completed", "phase": "open", "round": 2},
        at_ms=21_000,
    )
    assert "phase-pacing:2:open" not in projector.active_waits
    assert completed is not None
    assert completed.title == "全局执行错峰完成"


def test_projector_recovers_legacy_waits_and_retries_without_losing_state() -> None:
    restored = ExecutionProgressProjector.from_snapshot(
        {
            "current_run": "not-a-number",
            "completed_leg_quotes": {"ok": "2", "invalid": "-1"},
            "active_waits": [
                {},
                {"key": "bad", "updated_at_ms": "not-a-number"},
                {
                    "key": "valid",
                    "label": "仍在等待",
                    "updated_at_ms": 1_000,
                    "remaining_ms": 500,
                },
            ],
        }
    )
    assert restored.current_run == 0
    assert restored._completed_leg_quotes == {"ok": Decimal("2")}
    assert tuple(restored.active_waits) == ("valid",)
    assert ExecutionProgressProjector.from_snapshot(None).active_waits == {}

    events = (
        {"event": "leg_preparing", "round": 1, "sequence": 1, "symbol": "BTC", "action": "open"},
        {
            "event": "leg_waiting",
            "round": 1,
            "sequence": 1,
            "symbol": "BTC",
            "action": "open",
            "waiting_for": "order_identity",
        },
        {"event": "cycle_sizing_retry", "round": 1, "seconds": "1.5"},
        {"event": "cycle_read_retry", "round": 1, "seconds": "1.5", "read": "balance"},
        {"event": "cycle_read_retry", "round": 1, "seconds": "1.5", "read": "leverage"},
        {"event": "campaign_boundary_started"},
    )
    for offset, event in enumerate(events, start=1):
        assert restored.apply(event, at_ms=offset * 1_000) is None

    assert "campaign-boundary" in restored.active_waits
    assert (
        restored.apply({"event": "leg_progress", "progress_event": "submit", "round": 1, "sequence": 1}, at_ms=7_000)
        is not None
    )
    assert (
        restored.apply({"event": "leg_progress", "progress_event": "cancel", "round": 1, "sequence": 1}, at_ms=8_000)
        is not None
    )
    assert (restored.submissions, restored.cancels, restored.requotes) == (1, 1, 1)

    restored.apply(
        {"event": "leg_completed", "round": 1, "sequence": 2, "symbol": "SOL", "quote_volume": "invalid"},
        at_ms=9_000,
    )
    assert restored.snapshot()["execution_verified_quote_volume"] == "0"
