import sys

import pytest
from fxtr_testdeps.modulescoping.vardef import scoped_var

from fxtr.object_model import modulescoping

my_name = modulescoping.find_module_name_from_frame_at_stacklevel(1)  # should match __name__

value_one = scoped_var.get()  # should be None here

with scoped_var.locally_scoped_assign(value="tm2_scoped"):
    value_two = scoped_var.get()  # should be "tm2_scoped" here

value_three = scoped_var.get()  # should be None here

scoped_var.module_level_assign(__name__, value="tm2_mod_level")

assert "fxtr_testdeps.modulescoping.testmod2a" not in sys.modules
from fxtr_testdeps.modulescoping import testmod2a

assert "fxtr_testdeps.modulescoping.testmod2a" in sys.modules

my_name_from_imported_module = testmod2a.module_name_retriever()  # should still match __name__

value_four = scoped_var.get()  # should be tm2_mod_level here

with scoped_var.locally_scoped_assign(value="tm2_scoped2"):
    value_five = scoped_var.get()  # should be "tm2_scoped2" here

value_six = scoped_var.get()  # should be tm2_mod_level here

value_seven = (
    testmod2a.get_scoped_var()
)  # should be tm2_mod_level here even though it is inside another frame

with scoped_var.locally_scoped_assign(value="tm2_scoped3"):
    value_eight = scoped_var.get()  # should be "tm2_scoped3" here
    with pytest.raises(
        modulescoping.ModuleScopedAssignmentError,
        match="Cannot assign a module-level value while a local assignment is active!",
    ):
        from fxtr_testdeps.modulescoping import testmod2b

        del testmod2b

    with scoped_var.clear_locally_scoped_assignment():
        value_nine = scoped_var.get()  # should be tm2_mod_level here
        from fxtr_testdeps.modulescoping import testmod2b

        del testmod2b

        value_ten = scoped_var.get()  # should be tm2_mod_level here
    value_eleven = scoped_var.get()  # should be tm2_scoped3 here

value_twelve = scoped_var.get()  # should be tm2_mod_level here
