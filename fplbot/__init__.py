"""FPL Optimizer Bot — package root.

Pipeline modules:
  fpl_api      — read-only client for the official Fantasy Premier League API
  deadlines    — dynamic next-deadline computation + notification-window logic
  state        — load/save the persisted team state (state.json)
  predict      — expected-points model  P(minutes) x per-90 underlying rates
  optimizer    — MIP for the initial 15-man squad + starting XI + captain
  transfers    — multi-period rolling-horizon transfer solver (FT banking, hits, chips)
  captain      — rank-aware template-vs-differential captain choice
  strategy     — maps live overall rank -> differential/template risk knobs
  claude_review— Claude API sanity-check pass over the solver output + fresh news
  report       — turns a recommendation into an email (subject, body)
"""

__version__ = "0.1.0"
