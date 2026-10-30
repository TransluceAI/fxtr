# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportMissingParameterType=false
# pyright: reportUnusedImport=false
# isort: skip_file

# %% ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
from __future__ import annotations

if __name__ != "__main__":
    raise ValueError("This notebook is not meant to be imported")

import os

print(f"Running in {os.getcwd()} on {os.uname().nodename}")
print(f"This kernel is associated with file: {os.path.abspath(__file__)}")
print(f"CUDA_VISIBLE_DEVICES: {os.environ.get('CUDA_VISIBLE_DEVICES')}")

#! %reload_ext autoreload
#! %autoreload 2
#! %aimport -ray


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

import dotenv

dotenv.load_dotenv()

import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import nbutil

nbutil.set_up_treescope()

import logging

logging.basicConfig(level=logging.INFO)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

import dataclasses
import mashumaro
import typing


import fxtr2.data.entity_core as entity_core

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


@dataclasses.dataclass(frozen=True)
class MyAtom(mashumaro.types.SerializableType):
    value: bytes

    def _serialize(self):
        print(f"Serializing {self}")
        return self

    @classmethod
    def _deserialize(cls, atom):
        assert isinstance(atom, cls)
        print(f"Deserializing {atom}")
        return atom


@dataclasses.dataclass
class MyContainer:
    atoms: list[MyAtom]


@dataclasses.dataclass
class MyTopLevelContainer(mashumaro.DataClassDictMixin):
    container: MyContainer


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

ctr = MyTopLevelContainer(container=MyContainer(atoms=[MyAtom(value=b"hello")]))
ctr
# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

ctr.to_dict()

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

MyTopLevelContainer.from_dict(ctr.to_dict())

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


class MyTaggedBase(mashumaro.DataClassDictMixin):
    tag: typing.Final[str]

    class Config(mashumaro.config.BaseConfig):
        discriminator = mashumaro.types.Discriminator(  # <- add discriminator
            field="tag",
            include_subtypes=True,
        )


@dataclasses.dataclass
class MyTaggedFoo(MyTaggedBase):
    foo: str

    tag: typing.Final[str] = "foo"


@dataclasses.dataclass
class MyTaggedBar(MyTaggedBase):
    bar: int

    tag: typing.Final[str] = "bar"


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

MyTaggedFoo(foo="hello").to_dict()


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


@dataclasses.dataclass(frozen=True)
class MyBaseAtom:
    base_value: bytes


@dataclasses.dataclass(frozen=True)
class MyPyAtom(mashumaro.types.SerializableType):
    value: bytes

    def _serialize(self):
        print(f"Serializing {self}")
        return MyBaseAtom(b"ack:" + self.value)

    @classmethod
    def _deserialize(cls, atom):
        assert isinstance(atom, MyBaseAtom)
        assert atom.base_value.startswith(b"ack:")
        print(f"Deserializing {atom}")
        return cls(value=atom.base_value[4:])


class MyTaggedBase(mashumaro.DataClassDictMixin):
    tag: typing.Final[MyPyAtom]

    class Config(mashumaro.config.BaseConfig):
        discriminator = mashumaro.types.Discriminator(  # <- add discriminator
            field="tag",
            include_subtypes=True,
            variant_tagger_fn=lambda cls: cls.tag._serialize(),
        )


@dataclasses.dataclass
class MyTaggedFoo(MyTaggedBase):
    foo: str

    tag: typing.Final[MyPyAtom] = MyPyAtom(value=b"foo")


@dataclasses.dataclass
class MyTaggedBar(MyTaggedBase):
    bar: int

    tag: typing.Final[MyPyAtom] = MyPyAtom(value=b"bar")


MyTaggedFoo(foo="hello").to_dict()

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

MyTaggedBase.from_dict(MyTaggedFoo(foo="hello").to_dict())

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

entity_core.FxtrID.from_tag_and_data("f", b"hello")

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


@dataclasses.dataclass(kw_only=True)
class Foo:
    foo: str


@dataclasses.dataclass(kw_only=True)
class Bar(Foo):
    bar: int


import inspect

inspect.signature(Bar)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


@dataclasses.dataclass(frozen=True)
class MyDataclassEntity(
    entity_core.DataclassEntity,
    fxtr_type=entity_core.FxtrID(
        _raw_datahash=entity_core.FxtrID._format_tag_and_data("d", b"hello"),
    ),
):
    foo: str


inspect.signature(MyDataclassEntity)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

(
    MyDataclassEntity(foo="hello"),
    MyDataclassEntity(foo="hello").to_dict(),
    MyDataclassEntity(foo="hello")._to_fxtr_cbor(),
)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

entity_core.DataclassEntity.from_dict(MyDataclassEntity(foo="hello").to_dict())

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
MyDataclassEntity(foo="hello")


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


@dataclasses.dataclass(frozen=True)
class MySecondDataclassEntity(
    entity_core.DataclassEntity,
    fxtr_type=entity_core.FxtrID(
        _raw_datahash=entity_core.FxtrID._format_tag_and_data("d", b"bye"),
        _raw_nodehash=entity_core.FxtrID._format_tag_and_data("n", b"bye"),
    ),
):
    bar: int


MySecondDataclassEntity(bar=3)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

entity_core.DataclassEntity.from_dict(MySecondDataclassEntity(bar=3).to_dict())

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

entity_core._converter_for(MyDataclassEntity)(MyDataclassEntity(foo="hello").to_dict())

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

import mashumaro.codecs

mashumaro.codecs.BasicDecoder

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

ref = mashumaro.codecs.basic.decode(
    entity_core.FxtrID.from_tag_and_data("n", b"bye"), entity_core.EntityRef[MyDataclassEntity]
)

ref

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

r = entity_core.EntityRef[MyDataclassEntity](
    ref=entity_core.FxtrID.from_tag_and_data("n", b"bye"),
    converter=entity_core._converter_for(MyDataclassEntity),
)
r

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
r.__dict__

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


class Foo[T]:
    pass


foo = Foo[int]()
foo.__orig_class__
# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


@dataclasses.dataclass
class Bar[T]:
    bar: T


bar = Bar[int](bar=1)
bar.__orig_class__

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


@dataclasses.dataclass(frozen=True)
class Bar[T](mashumaro.types.GenericSerializableType):
    bar: T

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "__orig_class__":
            object.__setattr__(self, name, value)
        else:
            super().__setattr__(name, value)


bar = Bar[int](bar=1)
bar.__orig_class__


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


entity_core.FxtrID(
    _raw_datahash=entity_core.FxtrID._format_tag_and_data("d", b"12624datahash"),
    _raw_nodehash=entity_core.FxtrID._format_tag_and_data("n", b"3569nodehash"),
)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

id1 = entity_core.FxtrID(
    _raw_datahash=entity_core.FxtrID._format_tag_and_data("d", b"12624datahash"),
    _raw_nodehash=entity_core.FxtrID._format_tag_and_data("n", b"3569nodehash"),
)
id2 = entity_core.FxtrID(
    _raw_datahash=entity_core.FxtrID._format_tag_and_data("d", b"12624datahash"),
    _raw_nodehash=entity_core.FxtrID._format_tag_and_data("n", b"3qat46"),
)
id1 == id2, id1, id2

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

entity_core.FxtrID(
    _raw_datahash=entity_core.FxtrID._format_tag_and_data("z", b""),
    _raw_nodehash=entity_core.FxtrID._format_tag_and_data("c", b"3qat46"),
)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


import cbor2


@dataclasses.dataclass(frozen=True)
class CustomCborTag:
    value: bytes


def test_cbor_default_encoder(encoder, value):
    if isinstance(value, CustomCborTag):
        return encoder.encode(cbor2.CBORTag(33000, value.value))
    else:
        raise TypeError(f"Unsupported type: {type(value)}")


def test_cbor_tag_hook(decoder, tag, shareable_index=None):
    assert shareable_index is None
    if tag.tag == 33000:
        return CustomCborTag(value=tag.value)
    else:
        raise TypeError(f"Unknown tag: {tag.tag}")


tmp = cbor2.dumps(
    ["hello", CustomCborTag(value=b"bye")], default=test_cbor_default_encoder, canonical=True
)
tmp
# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

cbor2.loads(tmp)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

cbor2.loads(tmp, tag_hook=test_cbor_tag_hook)


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

ser = entity_core.fxtr_cbor_dumps(
    [
        MyDataclassEntity(foo="hello")._to_fxtr_cbor(),
        MySecondDataclassEntity(bar=3)._to_fxtr_cbor(),
    ]
)
ser

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

cbor2.loads(ser)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

entity_core.fxtr_cbor_loads(ser)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
import base64


def _b32enc(data: bytes) -> str:
    return base64.b32encode(data).decode().lower().rstrip("=")


_b32enc(cbor2.dumps([64, 126, 3, 64]))

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

[_b32enc(n.to_bytes(n.bit_length() // 8 + 1, "big")) for n in range(100)]


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

[n.to_bytes(n.bit_length() // 8 + 1, "big") for n in range(100)]


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


def encode_varint(n: int) -> bytes:
    parts = []
    while n >= 0x80:
        parts.append((n & 0x7F) | 0x80)
        n >>= 7
    parts.append(n)
    return bytes(parts)


def decode_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
    """Returns (value, new_position)"""
    result = 0
    shift = 0
    while data[pos] & 0x80:
        result |= (data[pos] & 0x7F) << shift
        shift += 7
        pos += 1
    result |= data[pos] << shift
    return result, pos + 1


[_b32enc(encode_varint(n)) for n in range(32 * 32 * 32)[::-1][:10]]

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

_b32enc(
    encode_varint(0)
    + encode_varint(3)
    + encode_varint(0)
    + encode_varint(14)
    + encode_varint(0)
    + encode_varint(48)
)


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


[_b32enc(encode_varint(3) + encode_varint(i)) for i in range(32)]

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

encode_varint(3)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

_VARINT32_ALPHABET = "0123456789abcdefghijklmnopqrstuv"


def _encode_varint32(value: int) -> str:
    if value < 0:
        raise ValueError("Only non-negative integers supported")
    result = []
    while True:
        chunk = value & 0xF  # Low 4 bits
        value >>= 4
        if value == 0:
            result.append(_VARINT32_ALPHABET[chunk])  # Terminal: 0-15
            break
        else:
            result.append(_VARINT32_ALPHABET[chunk + 16])  # Continuation: 16-31
    return "".join(result)


def _decode_varint32(s: str) -> tuple[int, str]:
    value = 0
    shift = 0
    for i, char in enumerate(s):
        idx = _VARINT32_ALPHABET.index(char)
        data = idx & 0xF  # Extract low 4 bits
        value |= data << shift
        shift += 4
        if idx < 16:  # Terminal character
            return value, s[i + 1 :]
    raise ValueError(f"Invalid varint32 string: {s}")


[_encode_varint32(i) for i in range(16 * 16)]

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
len("0123456789abcdefghijklmnopqrstuv")

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

_VARINT32_ALPHABET = "0123456789acdefghjklmnpqrstuvwxy"


def _encode_varint32(value: int) -> str:
    if value < 0:
        raise ValueError("Only non-negative integers supported")
    if value == 0:
        return _VARINT32_ALPHABET[0]

    # Collect nibbles from low to high
    nibbles = []
    while value > 0:
        nibbles.append(value & 0xF)
        value >>= 4

    # Reverse to get high-to-low order (big-endian)
    nibbles.reverse()

    # Encode: all but last are continuation (offset by 16), last is terminal
    result = []
    for i, nibble in enumerate(nibbles):
        if i < len(nibbles) - 1:
            result.append(_VARINT32_ALPHABET[nibble + 16])  # Continuation
        else:
            result.append(_VARINT32_ALPHABET[nibble])  # Terminal

    return "".join(result)


def _decode_varint32(s: str) -> tuple[int, str]:
    value = 0
    for i, char in enumerate(s):
        idx = _VARINT32_ALPHABET.index(char)
        data = idx & 0xF  # Extract low 4 bits (the actual nibble value)
        value = (value << 4) | data  # Shift left and add new nibble
        if idx < 16:  # Terminal character
            return value, s[i + 1 :]
    raise ValueError(f"Invalid varint32 string: {s}")


for i in range(0, 16 * 16 * 3, 16):
    for j in range(16):
        print(_encode_varint32(i + j), end=" ")
    print()


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
