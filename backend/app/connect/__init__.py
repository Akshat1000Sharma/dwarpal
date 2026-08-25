"""Connections: how somebody points their own agent at this merchant.

A connection is an identity and a delivery address, not a permission. It says "this agent is mine,
tell me on this number what it does". Purchasing authority comes from the credential chain and
from nowhere else, so a connection token can never widen what an agent is allowed to buy.
"""
