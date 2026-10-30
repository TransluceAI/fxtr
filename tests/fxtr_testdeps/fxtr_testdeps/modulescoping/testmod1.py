from fxtr_testdeps.modulescoping.vardef import scoped_var

from fxtr.object_model import modulescoping

my_name = modulescoping.find_module_name_from_frame_at_stacklevel(1)  # should match __name__

value_one = scoped_var.get()  # should be None here

with scoped_var.locally_scoped_assign(value="tm1_scoped"):
    value_two = scoped_var.get()  # should be "tm1_scoped" here

value_three = scoped_var.get()  # should be None here

scoped_var.module_level_assign(__name__, value="tm1_mod_level")

value_four = scoped_var.get()  # should be tm1_mod_level here

with scoped_var.locally_scoped_assign(value="tm1_scoped2"):
    value_five = scoped_var.get()  # should be "tm1_scoped2" here

value_six = scoped_var.get()  # should be tm1_mod_level here
