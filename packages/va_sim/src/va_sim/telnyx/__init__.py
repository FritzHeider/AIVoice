"""FakeTelnyx (specs/30-testing-and-evals.md §3.2): Telnyx-shaped REST API, signed webhooks, control routes.

``va_sim.telnyx.routes.telnyx_routes`` is everything FakeTelnyx serves beyond the shared ``/sim/*`` control
API. The simulator builds every wire message from the spec on its own; it shares no code with ``va_telnyx``.
"""
