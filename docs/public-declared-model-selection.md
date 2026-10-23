# Declared model metadata

`GET /v1/agents` and `GET /v1/sessions/{id}/agent` add `model_selection`
from the same spec already loaded for agent metadata. This adds no bundle,
configuration, environment, catalog, runner or provider reads.

`selection_authority=DECLARED_ONLY` describes the normalized parsed executor
field. It never proves a selected, available, current or served model;
`served_model` is always null. Deprecated `llm.model` can be lifted into the
executor field, so the logical source field does not claim original YAML origin.

Missing/default models remain `UNKNOWN_DEFAULT`. Known aliases and opaque
harness routes remain `UNKNOWN_OPAQUE_ROUTE`; dynamic or credential-shaped
values are omitted. Unreadable specs remain `UNKNOWN_UNREADABLE_SPEC`.
The projection never reads connection, auth, profile or free-form config.
This is metadata filtering for a typed model field, not a general-purpose
secret detector. Accepted model IDs are still declared intent only.

To inspect the source behavior without provider access, run:

```sh
PYTHONPATH=. python -m pytest -p no:rerunfailures -o addopts='' tests/server/routes/test_agent_model_selection.py -q
```

These fixtures include both in-memory HTTP resources, no extra cache loads,
credential-shaped values, opaque routes and legacy normalization. Runtime
adoption and actual selected-model provenance require separate review.
