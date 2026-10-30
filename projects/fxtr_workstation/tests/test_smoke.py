def test_pkg_importable() -> None:
    import fxtr_workstation as pkg

    assert pkg.__name__ == "fxtr_workstation"
