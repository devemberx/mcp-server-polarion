"""Route handlers, one module per domain. Import = register on ``ROUTER``.
Handlers stay thin: decode request -> ``World`` op -> ``wire`` shape.
"""

from . import documents, meta, test_runs, work_items

__all__ = ["documents", "meta", "test_runs", "work_items"]
