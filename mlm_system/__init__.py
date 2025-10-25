# bot/mlm_system/__init__.py
"""
MLM System - complete multi-level marketing implementation.
"""

# Services
from mlm_system.services.commission_service import CommissionService
from mlm_system.services.volume_service import VolumeService
from mlm_system.services.rank_service import RankService
from mlm_system.services.global_pool_service import GlobalPoolService

# Models and configuration
from mlm_system.config.ranks import Rank, RANK_CONFIG

# Utilities
from mlm_system.utils.time_machine import timeMachine

# Events
from mlm_system.events.event_bus import eventBus, MLMEvents

__all__ = [
    # Services
    'CommissionService',
    'VolumeService',
    'RankService',
    'GlobalPoolService',

    # Config
    'Rank',
    'RANK_CONFIG',

    # Utils
    'timeMachine',

    # Events
    'eventBus',
    'MLMEvents',
]