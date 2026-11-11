import os

# Tests never wait between fake executor calls.
os.environ.setdefault("ULTRON_PACE_SECONDS", "0")
