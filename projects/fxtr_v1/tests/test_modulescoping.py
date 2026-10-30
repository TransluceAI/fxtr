"""Tests for module scoping."""

import sys


def test_simple_scoping():
    from fxtr_testdeps.modulescoping import vardef

    assert vardef.scoped_var.get() is None

    assert "fxtr_testdeps.modulescoping.testmod1" not in sys.modules
    from fxtr_testdeps.modulescoping import testmod1

    assert testmod1.my_name == "fxtr_testdeps.modulescoping.testmod1"
    assert testmod1.value_one is None
    assert testmod1.value_two == "tm1_scoped"
    assert testmod1.value_three is None
    assert testmod1.value_four == "tm1_mod_level"
    assert testmod1.value_five == "tm1_scoped2"
    assert testmod1.value_six == "tm1_mod_level"

    assert vardef.scoped_var.get() is None


def test_nested_import_scoping():
    from fxtr_testdeps.modulescoping import vardef

    assert vardef.scoped_var.get() is None

    assert "fxtr_testdeps.modulescoping.testmod2" not in sys.modules
    from fxtr_testdeps.modulescoping import testmod2

    assert "fxtr_testdeps.modulescoping.testmod2" in sys.modules

    assert testmod2.my_name == "fxtr_testdeps.modulescoping.testmod2"
    assert testmod2.value_one is None
    assert testmod2.value_two == "tm2_scoped"
    assert testmod2.value_three is None
    assert testmod2.my_name_from_imported_module == "fxtr_testdeps.modulescoping.testmod2"
    assert testmod2.value_four == "tm2_mod_level"
    assert testmod2.value_five == "tm2_scoped2"
    assert testmod2.value_six == "tm2_mod_level"
    assert testmod2.value_seven == "tm2_mod_level"
    assert testmod2.value_eight == "tm2_scoped3"
    assert testmod2.value_nine == "tm2_mod_level"
    assert testmod2.value_ten == "tm2_mod_level"
    assert testmod2.value_eleven == "tm2_scoped3"
    assert testmod2.value_twelve == "tm2_mod_level"

    from fxtr_testdeps.modulescoping import testmod2a, testmod2b

    assert testmod2a.my_name == "fxtr_testdeps.modulescoping.testmod2a"
    assert testmod2a.value_one is None
    assert testmod2a.value_two == "tm2a_scoped"
    assert testmod2a.value_three is None
    assert testmod2a.value_four == "tm2a_mod_level"
    assert testmod2a.value_five == "tm2a_scoped2"
    assert testmod2a.value_six == "tm2a_mod_level"

    assert testmod2b.value_one == None
    assert testmod2b.value_two == "tm2b_mod_level"

    assert vardef.scoped_var.get() is None
