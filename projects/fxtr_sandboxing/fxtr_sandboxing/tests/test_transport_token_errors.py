"""Token error-path tests through the eryx WASM sandbox.

Validates that bad / already-consumed transport tokens surface clearly
as exceptions, instead of hanging or wedging the connection. The
sandbox-side ``redeem_explicit_token(token)`` method calls
``connect_transport(token)`` directly with whatever the test passes in,
bypassing the authorizer's mint step — so we can hand it a token that
will never validate.

Eryx-specific note: the failure path goes via the sync
``_transport_connect`` callback raising out of the eryx thread; the
exception propagates back into the sandbox's ``await
_transport_connect(...)`` call, which surfaces as an exception on the
sandbox side and via the RPC layer back to the test as an
``IRException`` with the message attached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import anyio.abc
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto, SandboxConnectProto
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.spawner import create_sandbox_environment

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


@service_protocol
class TransporterProto(Protocol):
    async def raw_echo(self, msg: str) -> str: ...
    async def http_get(self, path: str) -> str: ...
    async def redeem_explicit_token(self, token: str) -> str: ...


def _make_no_op_handler() -> object:
    """Build a sync transport handler that just closes the stream.

    The bad-token tests never actually invoke the handler, but
    ``add_raw_transport`` requires one to construct the authorizer.
    """

    def handler(stream: anyio.abc.ByteStream) -> None:
        # Defensive close in case the handler is invoked unexpectedly.
        # Best-effort — sync context can't aclose an async stream.
        return

    return handler


async def test_unknown_token_surfaces_clear_error() -> None:
    """A token that was never minted raises clearly out of connect_transport."""
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "transport_task")
        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            authorizer_ref = await env.add_raw_transport(_make_no_op_handler())  # type: ignore[arg-type]

            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
            )
            sandbox = await sandbox_ref.resolve()

            user_ref_any = await sandbox.connect(authorizer_ref)
            user_ref = user_ref_any.replace_service_type(TransporterProto)
            user = await user_ref.resolve()

            try:
                await user.redeem_explicit_token("not-a-real-token")
            except Exception as e:
                # The token-table miss raises BadTransportTokenError on
                # the host. That propagates back through the eryx
                # callback failure path, into the sandbox-side
                # connect_transport call, into raw_echo, and out through
                # the RPC layer as an IRException whose str() includes
                # the original message.
                assert "transport token unknown or already consumed" in str(
                    e
                ), f"unexpected error: {e!r}"
            else:
                raise AssertionError("expected an error for an unknown token")


async def test_double_consume_surfaces_clear_error() -> None:
    """Mint a token, redeem it, redeem the same token again — the
    second redemption raises BadTransportTokenError."""
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "transport_task")
        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            # Use a real handler this time so the first redemption
            # succeeds. The handler echoes (then closes) so raw_echo
            # returns normally on the first call.
            import anyio

            async def _echo(stream: anyio.abc.ByteStream) -> None:
                async with stream:
                    try:
                        while True:
                            chunk = await stream.receive(65536)
                            if not chunk:
                                break
                            await stream.send(chunk)
                    except anyio.EndOfStream:
                        pass

            async with anyio.create_task_group() as test_tg:

                def handler(stream: anyio.abc.ByteStream) -> None:
                    test_tg.start_soon(_echo, stream)

                authorizer_ref = await env.add_raw_transport(handler)
                spawner = await env.spawner_ref.resolve()
                sandbox_ref = await spawner.spawn(bundle)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
                )
                sandbox = await sandbox_ref.resolve()

                user_ref_any = await sandbox.connect(authorizer_ref)
                user_ref = user_ref_any.replace_service_type(TransporterProto)
                user = await user_ref.resolve()

                # Mint a token via the host's authorizer ref. Keep the
                # mint scope open across both redeem attempts —
                # otherwise the witness's end-callback revokes the
                # token before we get to the first redeem.
                async with FxtrScope(name="mint") as mint_scope:
                    authorizer_typed = authorizer_ref.replace_service_type(
                        NetworkHandleAuthorizerProto
                    )
                    authorizer = await authorizer_typed.resolve(mint_scope)
                    token, _witness = await authorizer.get_transport_token()

                    # First redemption succeeds (token is fresh).
                    got = await user.redeem_explicit_token(token)
                    assert got == "hello", f"got {got!r}"

                    # Second redemption — the token-table consume has
                    # already happened, so this raises
                    # BadTransportTokenError out of the eryx callback.
                    try:
                        await user.redeem_explicit_token(token)
                    except Exception as e:
                        assert "transport token unknown or already consumed" in str(
                            e
                        ), f"unexpected error: {e!r}"
                    else:
                        raise AssertionError("expected an error for a re-used token")

                test_tg.cancel_scope.cancel()
