"""Iris real-time accuracy bench.

Drives Iris primitives against an instrumented target window and measures
where clicks actually land vs where they were supposed to land. The harness
reports back via a JSONL event log so we don't have to interpret pixels to
know whether a click hit.
"""
