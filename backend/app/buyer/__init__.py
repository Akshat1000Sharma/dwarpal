"""The buyer's agent: the other side of the counter.

This is deliberately the mirror image of the merchant's gate. Here a model is in charge, because
choosing what to buy is a judgement call and getting it wrong costs the buyer a wrong item. On the
merchant's side of `app/checkout/complete.py` no model is consulted about money at all, because
getting that wrong costs somebody else their money.

Nothing in this package is importable from `app/kernel/`, and `tests/test_kernel_isolation.py`
fails the build if that ever stops being true.
"""
