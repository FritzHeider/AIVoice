"""VoiceAgents pathway text runs (specs/12 §7, §8): the test chat's conversation and the unit-test runner.

Shared by the api (test chat, publish regression) and the worker (``PathwayTestWorkflow``). The package
``__init__`` imports nothing so workflow code can import submodules without side effects.
"""
