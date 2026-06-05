"""ML / analytics helpers.

Distinct from `nlp/` (intent + entity extraction) and `banking/` (mock core
operations): these modules pattern-mine the user's transaction history to
surface findings and predictions — spend deltas, anomalies, subscription-like
recurring charges, recipient suggestions, amount predictions.
"""

# Submodules import lazily — eager imports here would circular-trigger
# store ↔ banking ↔ schemas on package init.
