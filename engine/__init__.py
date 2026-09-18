# Marks `engine` as a package. Each module inside corresponds to one stage of
# the pipeline described in the spec:
#
#   Data Collection -> Performance History -> Historical Learning ->
#   Pricing Decision -> Price Execution -> Outcome Evaluation -> Learning
#
#   engine/ingestion.py   -> Data Collection (validation + storage)
#   engine/history.py     -> Performance History (raw sales -> per-cycle rollups)
#   engine/eligibility.py -> guards that gate which products the Pricing
#                             Decision stage is even allowed to touch
#   engine/learning.py    -> Historical Learning + Outcome Evaluation
#                             (what happened last time we changed this price?)
#   engine/decision.py    -> Pricing Decision (INCREASE / DECREASE / HOLD)
#   engine/execution.py   -> Price Execution (apply it, log it, prevent duplicates)
#
# Keeping these as separate modules (rather than one big "pricing.py") mirrors
# the spec's explicit requirement that the stages be "conceptually separate...
# so that pricing decisions are auditable and the learning system can be
# improved without compromising transactional systems."
