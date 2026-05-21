---
identifier: '003'
kind: improvement
severity: medium
status: open
title: No end-to-end test exercises omnibus mounting of breadcrumbs+memory+search
---
185 tests, all green, but each kind server is tested in isolation. No test instantiates a Server, merges all three kind registries (the omnibus deployment path), and asserts the resulting tool surface. A test of this shape would have caught BC-001 (silent trace_graph shadowing) and BC-004 (resource-handler dedupe gap) before they shipped.

README recommends omnibus mode as the default for <32 GB RAM — the untested path is the recommended path.

Add tests/test_omnibus.py: (1) mount all three; assert tools/list contains expected union with both kinds' trace_graph reachable per BC-001's resolution; (2) confirm each resource prefix registered exactly once after merge (BC-004); (3) dispatch one tool from each kind via merged registry. Use existing testcontainers Postgres fixture. Related: BC-001, BC-004.