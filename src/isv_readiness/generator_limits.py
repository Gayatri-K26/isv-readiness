"""Shared wall-clock limits for generator adapters and their model calls."""

# Codex makes one schema-constrained call. Claude may make two local
# schema-correction attempts. Keep either route plus two minutes of
# adapter/serialization headroom inside the outer boundary.
CODEX_MODEL_TIMEOUT_SECONDS = 1680
CLAUDE_MODEL_ATTEMPT_TIMEOUT_SECONDS = 840
GENERATOR_ADAPTER_TIMEOUT_SECONDS = 1800
MAX_GENERATOR_TIMEOUT_SECONDS = 28_800
MAX_GENERATOR_REQUEST_BYTES = 20_000_000
