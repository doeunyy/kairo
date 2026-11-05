"""Kairo's deterministic mail pipeline.

The LLM only fills a fixed extraction schema. Every decision that changes the
application tracker (which application, which status, which dates) is made here,
in code, so instructions hidden in an email have no path to a write.
"""

__version__ = "0.1.0"
