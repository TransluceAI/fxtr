"""Basic smoke tests to verify the package is set up correctly."""


def test_import():
    import fxtr_workflow

    assert fxtr_workflow is not None


def test_fxtr2_dependency():
    """Verify that fxtr2 is importable (linked as a dependency)."""
    import fxtr2

    assert fxtr2 is not None
