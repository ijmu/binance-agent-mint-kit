"""mintkit — shared local helpers for the free-mint pipeline.

Modules:
  chain    BSC JSON-RPC reads with endpoint rotation (stdlib only).
  verify   Sourcify + GoPlus checks for a contract address.
  social   X/Twitter mirror reads + status-id snowflake decoding.
  registry minted_registry.json — what this wallet already minted.
  logs     atomic one-line log appends with optional dedupe key.
  round    one-shot round runners (incremental / publish checks).
  factorywatch  on-chain factory event watch (P2, SHADOW ONLY: logs to
                hidden_files/factory-shadow-log.md, never feeds the board).
"""

__version__ = "0.1.0"
