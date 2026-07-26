"""Composed beta-volume execution service."""

from __future__ import annotations

from .cycles import CycleCheckpointMixin, CycleLoopMixin
from .execution.accounting import ExecutionAccountingMixin
from .execution.flatten import PositionFlatteningMixin
from .execution.flow import ExecutionFlowMixin
from .execution.leg_reconciliation import LegReconciliationMixin
from .execution.legs import LegExecutionMixin
from .execution.pair import PairExecutionMixin
from .execution.stopping import SafetyStopMixin
from .recovery import RecoveryMixin
from .runtime import RuntimeMixin


class LiveBetaVolumeService(
    RuntimeMixin,
    RecoveryMixin,
    CycleLoopMixin,
    CycleCheckpointMixin,
    ExecutionFlowMixin,
    SafetyStopMixin,
    ExecutionAccountingMixin,
    PairExecutionMixin,
    PositionFlatteningMixin,
    LegExecutionMixin,
    LegReconciliationMixin,
):
    PAIR_HEARTBEAT_SECONDS = 5.0
