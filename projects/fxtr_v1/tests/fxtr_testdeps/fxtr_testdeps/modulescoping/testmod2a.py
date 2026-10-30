import sys

from fxtr_testdeps.modulescoping.vardef import scoped_var

from fxtr.object_model import modulescoping

assert "fxtr_testdeps.modulescoping.testmod2" in sys.modules


def module_name_retriever():
    return modulescoping.find_module_name_from_frame_at_stacklevel(2)


my_name = module_name_retriever()  # should match __name__

value_one = scoped_var.get()  # should be None here

with scoped_var.locally_scoped_assign(value="tm2a_scoped"):
    value_two = scoped_var.get()  # should be "tm2a_scoped" here

value_three = scoped_var.get()  # should be None here

scoped_var.module_level_assign(__name__, value="tm2a_mod_level")

value_four = scoped_var.get()  # should be tm2a_mod_level here

with scoped_var.locally_scoped_assign(value="tm2a_scoped2"):
    value_five = scoped_var.get()  # should be "tm2a_scoped2" here

value_six = scoped_var.get()  # should be tm2a_mod_level here


def get_scoped_var():
    return scoped_var.get()  # should return the _caller's_ value, not tm2a_scoped2
