"""DEPRECATED: The breadcrumbs table has been dropped (Plan 008 Tier A).
BreadcrumbModel has been removed; the breadcrumb CLI now delegates to WorkItemModel.
All breadcrumb tests are skipped.
"""

import pytest

pytestmark = pytest.mark.skip(
    reason="BreadcrumbModel removed; breadcrumb CLI delegates to WorkItemModel"
)
