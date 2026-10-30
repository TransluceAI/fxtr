import sys

from fxtr_testdeps.modulescoping.vardef import scoped_var

assert "fxtr_testdeps.modulescoping.testmod2" in sys.modules

value_one = scoped_var.get()  # should be None here

scoped_var.module_level_assign(__name__, value="tm2b_mod_level")

value_two = scoped_var.get()  # should be tm2b_mod_level here
