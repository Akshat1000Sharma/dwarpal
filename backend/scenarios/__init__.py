"""The scenario suite: Dwarpal exercised over HTTP, the way anything else would use it.

The unit tests prove each guarantee in isolation with the application in-process. This proves the
same guarantees hold through the real HTTP surface, under concurrency, across many agents, and for
long enough that state accumulates. It is also how the merchant dashboard gets filled with data:
every scenario leaves real verdicts, mandates, evidence packets and disputes behind.

Nothing here targets any external system. Every request is fired at Dwarpal's own door.
"""
