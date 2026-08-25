"""Outbound purchase receipts over WhatsApp.

Separate from ``app.escalation`` because the two answer different questions. An escalation asks
the human to decide something. A receipt tells them what an agent already did on their behalf,
whether that was a purchase or a refusal, and is never a request for input.
"""
