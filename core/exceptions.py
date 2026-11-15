"""Core Domain Exceptions for Korea Market Trading Engine."""

class TradingEngineError(Exception):
    """Base exception for all trading engine errors."""
    pass

class ConfigurationError(TradingEngineError):
    """Raised when environment or settings configuration is invalid."""
    pass

class OrderRejectedError(TradingEngineError):
    """Raised when an order is rejected by broker or risk manager."""
    pass

class LookaheadViolationError(TradingEngineError):
    """Raised when an algorithm attempts to reference future or incomplete candle data."""
    pass

