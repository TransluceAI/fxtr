"""View-runtime tests: shadows + concrete views over a fake backend that
runs the real registry + canonicalizer (regularity path exercised end to
end from view objects)."""

import pytest
from frozendict import frozendict

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import ViewAxisError
from fxtr_experiment_task.runner_api import ViewSel, WiredArg
from fxtr_experiment.spec_registry import SpecRegistry
from fxtr_experiment_task.view_runtime import RuntimeDataView, RuntimeSubtreeView, build_view
from fxtr_experiment_task.view_spec import KeepName, SubtreeViewSpec, open_child

SOME_ID = enc.store_config(m.DataGridConfig(axes=()))

ROOT_SPEC = SubtreeViewSpec(path=(), target_axes=(), axes=(), mode="write")


class FakeBackend:
    """Registry + canonicalizer, storage stubbed; records effectful ops."""

    def __init__(self) -> None:
        self.reg = SpecRegistry()
        self.configs: dict[str, m.TaskGridConfig] = {}
        self.reads: list[ViewSel] = []
        self.writes: list[tuple[ViewSel, object]] = []

    async def read_cell_entity(self, sel):
        self.reads.append(sel)
        return SOME_ID

    async def read_cell_frame(self, sel):
        raise NotImplementedError("the fake backend serves no frames")

    async def read_handle_key(self, sel):
        self.reads.append(sel)
        return SOME_ID

    async def write_cell_entity(self, sel, value):
        self.writes.append((sel, value))

    async def write_cell_frame(self, sel, frame):
        raise NotImplementedError("the fake backend serves no frames")

    async def gather(self, sel, keys):
        raise NotImplementedError("the fake backend serves no frames")

    def _open(self, subtree, key, axes, kind, mode: "m.SliceMode | None" = None):
        subtree_spec = self.reg.sliced(subtree)
        if mode is None:  # opens follow the chain: inherit the subtree's authority
            mode = subtree_spec.mode
        spec = open_child(subtree_spec, key, tuple(axes), kind, mode=mode)
        return self.reg.mint(spec)

    async def open_data(self, subtree, key, axes):
        return self._open(subtree, key, axes, "data")

    async def open_dataframe(self, subtree, key, axes, schema):
        raise NotImplementedError("the fake backend serves no dataframes")

    async def open_handle(self, subtree, key, axes):
        return self._open(subtree, key, axes, "handle", "read")

    async def open_subtree(self, subtree, key, axes):
        return self._open(subtree, key, axes, "subtree")

    async def open_checkpoint(self, subtree, key, axes):
        return self._open(subtree, key, axes, "checkpoint", "write")

    async def open_task(self, subtree, key, axes, task_fn, args, outputs):
        config, grant = self.reg.open_task(subtree, key, axes, task_fn, args, outputs)
        self.configs[key] = config
        return grant

    async def run_step(self, checkpoint, step, args):
        raise NotImplementedError("the fake backend runs no steps")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


async def _mh_flat(backend: FakeBackend):
    root = build_view(backend, backend.reg.mint(ROOT_SPEC))
    assert isinstance(root, RuntimeSubtreeView)  # the root grant is a subtree
    judges = await root.open_handle("judges", axes=(m.AxisSpec("judge", "str"),))
    transcripts = await root.open_data(
        "transcripts",
        axes=(m.AxisSpec("category", "str"), m.AxisSpec("ix", "int")),
    )
    ws = await root.open_subtree("mh", axes=())
    grades = await ws.open_task(
        "grades",
        axes=(m.AxisSpec("category", "str"), m.AxisSpec("judge", "str")),
        task_fn=judges,
        args={"transcripts": transcripts.read_only()},
    )
    return root, judges, transcripts, grades


@pytest.mark.anyio
async def test_open_task_reproduces_canonical_rule(backend: FakeBackend) -> None:
    """The smoke-test rule, re-derived through view objects end to end."""
    await _mh_flat(backend)
    config = backend.configs["grades"]
    assert config.rule.task_fn == m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="judges"),),
        target_axes=(m.Param(1),),
    )
    binding = config.rule.inputs["transcripts"]
    assert isinstance(binding, m.SliceBinding)
    assert binding.mode == "read"  # read_only() downgrade landed in identity
    assert binding.received_axis_names == ("ix",)
    assert binding.slice == m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="transcripts"),),
        target_axes=(m.Param(0), m.KeepAxis()),
    )
    assert config.outputs == m.DEFAULT_TASK_OUTPUTS


@pytest.mark.anyio
async def test_reads_writes_emit_positional_sels(backend: FakeBackend) -> None:
    _, judges, transcripts, grades = await _mh_flat(backend)
    await transcripts.write(SOME_ID, category="c", ix=3)
    ((sel, value),) = backend.writes
    assert value == SOME_ID
    assert dict(sel.bound) == {0: "c", 1: 3}

    renamed = transcripts.read_only().rename_axes(cat="category")
    await renamed.at(cat="c").read(ix=7)
    assert dict(backend.reads[-1].bound) == {0: "c", 1: 7}

    out = grades.output()  # sole declared output resolves to "result"
    assert isinstance(out, RuntimeDataView)
    await out.read(category="c", judge="j")
    sel = backend.reads[-1]
    assert sel.output == "result" and dict(sel.bound) == {0: "c", 1: "j"}

    key = await judges.read_key(judge="j1")
    assert key == SOME_ID and dict(backend.reads[-1].bound) == {0: "j1"}


@pytest.mark.anyio
async def test_view_layer_validation(backend: FakeBackend) -> None:
    root, judges, transcripts, _ = await _mh_flat(backend)
    with pytest.raises(ViewAxisError):  # kind mismatch
        transcripts.at(ix="not-an-int")
    with pytest.raises(ViewAxisError):  # absent axis
        transcripts.at(nope=1)
    with pytest.raises(ViewAxisError):  # single cell required
        await transcripts.read_only().read(category="c")
    with pytest.raises(ViewAxisError):  # subtree views cannot be sliced
        root.at(x=1)
    variants = await root.open_subtree("variants", axes=(m.AxisSpec("variant", "str"),))
    with pytest.raises(ViewAxisError):  # child axis collides with prefix name
        await variants.open_data("d", axes=(m.AxisSpec("variant", "str"),))
    # ...rename the *view* first, exactly as documented:
    renamed = variants.rename_axes(v="variant")
    await renamed.open_data("d", axes=(m.AxisSpec("variant", "str"),))


@pytest.mark.anyio
async def test_registry_rejects_mode_upgrade(backend: FakeBackend) -> None:
    root, *_ = await _mh_flat(backend)
    # A read-mode child grant (a downgrade under the write root); the point of
    # the test is that the registry refuses to *upgrade* it back at wiring time.
    read_grant = backend._open(
        ViewSel(token=root._shadow.grant.token), "ro", (m.AxisSpec("x", "str"),), "data", "read"
    )
    with pytest.raises(ViewAxisError):
        backend.reg.wired_spec(
            WiredArg(
                sel=ViewSel(token=read_grant.token),
                wiring=(KeepName("x"),),
                mode="write",
            )
        )


@pytest.mark.anyio
async def test_nested_prefix_names_overlay(backend: FakeBackend) -> None:
    """Child views inherit the holder's *local* names for prefix axes."""
    root = build_view(backend, backend.reg.mint(ROOT_SPEC))
    assert isinstance(root, RuntimeSubtreeView)
    variants = await root.open_subtree("variants", axes=(m.AxisSpec("variant", "str"),))
    inner = await variants.rename_axes(v="variant").open_data("cfg", axes=(m.AxisSpec("x", "str"),))
    assert [a.name for a in inner.axes] == ["v", "x"]
    await inner.write(SOME_ID, v="v1", x="a")
    ((sel, _),) = backend.writes
    assert dict(sel.bound) == {0: "v1", 1: "a"}
