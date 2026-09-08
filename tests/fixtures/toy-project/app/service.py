from app.models import User


def greet(user: User) -> str:
    return f"hi {user.name}"
