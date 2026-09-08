import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.models import User  # noqa: E402
from app.service import greet  # noqa: E402


def test_greet_says_hello():
    assert greet(User("Ann")) == "hello Ann"


def test_greet_uses_name():
    assert "Bob" in greet(User("Bob"))


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                failed += 1
                print("FAIL", name, e)
    sys.exit(1 if failed else 0)
