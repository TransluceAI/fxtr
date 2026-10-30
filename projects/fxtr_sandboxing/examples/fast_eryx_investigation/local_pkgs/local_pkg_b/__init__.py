"""A second tiny local package, to make sure mounting >1 works."""

from local_pkg_a import hello as a_hello


def combined() -> str:
    return f"local_pkg_b->{a_hello()}"
