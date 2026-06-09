"""DEPRECATED: The migration has been completed and the breadcrumbs table dropped.
BreadcrumbModel no longer exists. All tests skipped.
"""

import pytest

pytestmark = pytest.mark.skip(reason="BreadcrumbModel removed; migration complete")
