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
