"""Top-level shim that satisfies the static-submission contract.

Usage:
    from bot import compose
    msg = compose(category_dict, merchant_dict, trigger_dict, customer_dict_or_none)
"""

from vera_bot.compose import compose

__all__ = ["compose"]
