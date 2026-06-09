"""DEPRECATED: The breadcrumbs table has been dropped (Plan 008 Tier A).
BreadcrumbModel.reconcile_with_git is now handled by the breadcrumb CLI's
reconcile subcommand, which uses WorkItemModel. All tests skipped.
"""

import pytest

pytestmark = pytest.mark.skip(reason="BreadcrumbModel removed; reconcile now uses WorkItemModel")
