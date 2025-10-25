# bot/mlm_system/events/event_bus.py
"""
Event bus for decoupled communication between components.
Ready for future microservices architecture.
"""
from typing import Dict, List, Callable, Any
import logging
import asyncio

logger = logging.getLogger(__name__)


class EventBus:
    """
    Simple event bus implementation.
    Can be replaced with RabbitMQ/Kafka in future.
    """

    _instance = None
    _handlers: Dict[str, List[Callable]] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._handlers = {}
        return cls._instance

    def subscribe(self, eventName: str, handler: Callable):
        """Subscribe handler to event."""
        if eventName not in self._handlers:
            self._handlers[eventName] = []

        self._handlers[eventName].append(handler)
        logger.debug(f"Handler {handler.__name__} subscribed to {eventName}")

    def unsubscribe(self, eventName: str, handler: Callable):
        """Unsubscribe handler from event."""
        if eventName in self._handlers:
            self._handlers[eventName].remove(handler)
            logger.debug(f"Handler {handler.__name__} unsubscribed from {eventName}")

    async def emit(self, eventName: str, data: Dict[str, Any]):
        """Emit event to all subscribers."""
        if eventName not in self._handlers:
            return

        logger.debug(f"Emitting event {eventName} with data: {data}")

        for handler in self._handlers[eventName]:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                logger.error(f"Error in handler {handler.__name__} for event {eventName}: {e}")

    def clear(self):
        """Clear all event handlers."""
        self._handlers.clear()


# Global event bus instance
eventBus = EventBus()


# Predefined events
class MLMEvents:
    """Standard MLM system events."""

    PURCHASE_COMPLETED = "purchase.completed"
    COMMISSION_CALCULATED = "commission.calculated"
    RANK_ACHIEVED = "rank.achieved"
    RANK_ASSIGNED = "rank.assigned"

    VOLUME_UPDATED = "volume.updated"
    USER_ACTIVATED = "user.activated"
    USER_DEACTIVATED = "user.deactivated"

    MONTH_STARTED = "month.started"
    MONTH_ENDED = "month.ended"

    GLOBAL_POOL_CALCULATED = "global_pool.calculated"
    GLOBAL_POOL_DISTRIBUTED = "global_pool.distributed"

    PIONEER_BONUS_GRANTED = "pioneer_bonus.granted"
    REFERRAL_BONUS_PAID = "referral_bonus.paid"