"""ultron: Claude plans and accepts, an executor via Antigravity CLI (agy) works.

The contract is TaskSpec -> Executor -> TaskResult. Only the compact view of a
TaskResult ever reaches Claude's context; everything else lives in state.
"""

__version__ = "0.1.0"
