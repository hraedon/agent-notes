"""Work-item server model — split into a subpackage (Plan 008 P0).

This subpackage holds the implementation of the work-item CRUD/query/lease
operations. The public surface is still ``WorkItemModel`` in
``agent_notes.core.work_item_model``; these modules are internal.

Modules:
- ``_common``   — shared helpers (workspace/vocab lookup, embedding diff,
                  regista-snapshot mirroring, change-log payload builder).
- ``_regista``  — regista-face write path (the converged store is SoT).
- ``_native``   — native op-log write path (degrade mode, no regista).
- ``_queries``  — read-only queries + delete + diagnose.
- ``_cross_project`` — P3 request / wait / cross-project links.
"""

from __future__ import annotations
