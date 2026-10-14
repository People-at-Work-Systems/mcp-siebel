"""Generic tool executors.

Each module here implements one ``kind`` of tool (a small, generic executor) and
registers it with the kind registry in ``tools.py``. Executors are code resolved
at import time; only the JSON spec is hot-reloaded.
"""
