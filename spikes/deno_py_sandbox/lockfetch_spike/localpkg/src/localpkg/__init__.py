from .borrowed_module import BORROWED


def probe() -> str:
    return f"localpkg ok; {BORROWED}"
