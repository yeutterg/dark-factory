"""Decision agents: typed questions in, a choice and a confidence out.

A decider is a JSON command (``[commands.decider]``) answering operation
``decide`` with input ``{"state": {...}, "questions": {name: {"type": "choice",
"instructions": str, "options": {choice: description}}}}``. Its evidence is
``{"agent": str, "model": str, "answers": {name: {"choice": str,
"confidence": float, "probabilities": {choice: float}}}}``.

Answers are advice. The controller applies its own limits, and a missing,
failed or unconfident decider falls back to rules; it never stops the factory.
"""
