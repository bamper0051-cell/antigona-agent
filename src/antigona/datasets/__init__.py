"""Datasets for intent classification, adversarial testing, and regression.

This package provides structured datasets used by the quality pipeline:

- intent_examples: Balanced curriculum for IntentRouter training/evaluation
- adversarial_examples: Prompt injection, mixed intents, indirect requests
- correction_examples: User corrections → regression tests

Each module exports a ``load()`` function returning a list of dicts with
standardised fields (``text``, ``expected_intent``, ``tags``).
"""

from __future__ import annotations

from antigona.datasets.adversarial import adversarial_examples
from antigona.datasets.corrections import correction_examples
from antigona.datasets.intents import intent_examples

__all__ = ["intent_examples", "adversarial_examples", "correction_examples"]
