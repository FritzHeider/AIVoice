"""FakeStripe (specs/30-testing-and-evals.md §4.1; Decision P03-T07a): the Stripe REST subset the billing
provider uses, signed webhooks, and the ``/sim/stripe/*`` control API.

Kept import-free: ``va_sim.state`` imports the store and the webhook emitter, and the handler modules import
``SimState`` only for type checking.
"""
