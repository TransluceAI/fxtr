from fxtr.object_model import collection_tracking, core

with collection_tracking.define_collection_module(
    __name__, namespace_id="!testing.collection_module_example"
):

    class SomeDataEntity(core.DataEntityBase, type_id="SomeDataEntity^1"):
        foo: int

    bar = SomeDataEntity(4)
    baz = {"qux": core.AdHocMetadata(metadata="someattr")}

    _private_entity = SomeDataEntity(5)
