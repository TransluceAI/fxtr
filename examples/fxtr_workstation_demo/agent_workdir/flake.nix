{
  description = "fxtr_workstation_demo agent workdir — nix-backed service + DATA entity + the `default` UI preset.";

  # The fxtr library flake. Placeholder in the committed template;
  # `prepare_workdir_repo` (scaffold_workdir.py / tests) rewrites it to a
  # pinned git ref in the runnable workdir copy.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;

      # ONE aggregator venv (packages/{presentables,transcripts,docent_fxtr_proxy}
      # made importable; fxtr2-only, light) — the single mkVenv root for every
      # agent-owned typeEntity + the demo_note DATA entity. See
      # type_entities/pyproject.toml.
      typeEntitiesVenv = lib.mkVenv {
        name = "fxtr-demo-type-entities-venv";
        workspaceRoot = ./type_entities;
      };

      # forType = { idString; entity }. The hand-pasted idString is the preset
      # key + what the dispatcher's eager validation checks the built typeEntity
      # against (print from a run_script: `print(MyType.fxtr_type.id_string())`).
      mkType = idString: fromPython: {
        inherit idString;
        entity = lib.mkEntityFromPython {
          venv = typeEntitiesVenv;
          inherit fromPython;
        };
      };
      noteType = mkType "dmtflbiypxdyg7kitewxermpdglbiecovlddun2pdknfhtowen5ca" "presentables:Note.fxtr_type";
      transcriptType = mkType "dne4zrdivvg3dmctwoy52mecjfw6glvr7nzkmqnua67fgeh7pdrha" "transcripts:Transcript.fxtr_type";
      dqlResultType = mkType "dgjylbhgs6zxjagnssscboj3arshmngzwiquleyo3t7yykvh6bd4q" "docent_fxtr_proxy:DQL_RESULT_TYPE_ID";
      dqlSchemaType = mkType "dbbvgym62axsixyy46wt7mfhnybd3kkc3dlv6ekmfzzt6r24ets3a" "docent_fxtr_proxy:DQL_SCHEMA_TYPE_ID";
      metadataFieldsType = mkType "dwsehoi3p3nahjcj4imegr6ucqfhz5nke2gumvgtp6eggbjo74shq" "docent_fxtr_proxy:METADATA_FIELDS_TYPE_ID";
      # A SERVICE type id (`_service_type_id()`), not an entity's — the
      # dispatcher routes service refs through the same type-id table. The
      # host-importable half of packages/delayed_echo provides the constant.
      delayedEchoType = mkType "dpqh2nzfeychb4d45zivvwye465puwafhsrscsovzfitqq62idhla" "delayed_echo:DELAYED_ECHO_TYPE_ID";
      # The host-side demo-driver helper SERVICE type id (packages/demo_helper).
      # Its renderer is the self-contained auto-driver UI.
      demoHelperType = mkType "dvtsrume4lojlinae4jbn4jdsdf7eemqxis3p4ryuo2ops5se4aqq" "demo_helper:DEMO_HELPER_TYPE_ID";
      # Simple-workflow demo types: the scheduler's SERVICE type id plus the
      # entity types its runs produce/reference (packages/simple_tasks).
      schedulerType = mkType "dtzk6ihnud4qsyhsvv6oxngnqvi4gbl7hemrifocoevmsupaxn4uq" "simple_tasks:SIMPLE_SCHEDULER_TYPE_ID";
      # The per-run output channel's tag entity type (stage 13); the
      # run-grid card dispatches on it via ChannelContentCard.
      schedulerRunTagType = mkType "diew446am36h3re2eg6beyzvrjytthkgjna6k74w7b6z2n24lrifa" "simple_tasks:SchedulerRunTag.fxtr_type";
      # Streaming (nested-channel) demo types — stage 13 part 4: the
      # streaming scheduler's SERVICE type plus its run / per-cell
      # progress tag entity types.
      streamingSchedulerType = mkType "dih37n6u2qjxtbtxgif5rtszkewx6r34st3b3ty7j2wv5bowvtbgq" "streaming_tasks:STREAMING_SCHEDULER_TYPE_ID";
      streamingRunTagType = mkType "d23e4zdt5uswuxfbmv4hbkyd6zrnhzq563h2bzhajxsqhpsfhh63a" "streaming_tasks:StreamingRunTag.fxtr_type";
      taskProgressTagType = mkType "dqgoq3rmzwfyjj6kbacvujotntxdhxoj77t6tkcwfrqe5lokav6uq" "streaming_tasks:TaskProgressTag.fxtr_type";
      runResultType = mkType "dtg4srbxk5vb7o5wh5ukmyzobbkyssjewbjb54gdwfojbpr35zb3q" "simple_tasks:SimpleRunResult.fxtr_type";
      noteWithLinksType = mkType "d77kuknipdn5my5reahuoc5jyw34wpshyamr2teedhw64ml7j3tyq" "simple_tasks:NoteWithLinks.fxtr_type";
      promptTemplateType = mkType "db5uvxtdwrsgna2cqgz2lav7ar34nobm5baaidsqthisw5l67w36q" "simple_tasks:PromptTemplate.fxtr_type";

      # Dispatch-key UUIDs shared across the renderer slices. Lifted here so the
      # constants live in one place and are `inherit`ed into each slice rather
      # than re-pasted per renderer. (Named `keyIds` to avoid clashing with the
      # per-subtype `forDispatchKeys` list field, which selects from these.)
      keyIds = {
        fullPanel = "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234";
        blockLink = "b3c4d5e6-7f89-4a01-9b23-4c5d6e7f8901";
        inlineLink = "c4d5e6f7-8901-4a12-9b34-5d6e7f890123";
        hoverPreview = "d5e6f7a8-9012-4a23-9b45-6e7f89012345";
        streamEntry = "4b2e1a3c-7d68-4e91-a25f-9c3d8b0e1f7a";
        serviceInlineLink = "f1e2d3c4-b5a6-4798-8c1d-2e3f405162a7";
        figure = "f4a3e2c1-9d0b-4d8a-9e2c-5f1b3a8e0d12";
        # @fxtr-ui/rd-types-channel's ChannelContentCard (stage 13).
        channelContentCard = "830e2987-08a7-410f-8c23-e01f05878c50";
      };

      # Each renderer dir carries its own `fxtr_renderer.nix` slice (the
      # successor to its old fxtr_renderer.json). Merge them into one mkUiPreset
      # call — entry names are unique so `//` is the right merge; mkUiPreset
      # still throws on a duplicate (type-id, dispatch-key) across slices.
      rendererEntries =
        (import ./renderers/builtin/fxtr_renderer.nix { inherit lib keyIds; })
        // (import ./renderers/note/fxtr_renderer.nix { inherit lib noteType keyIds; })
        // (import ./renderers/transcript/fxtr_renderer.nix { inherit lib transcriptType keyIds; })
        // (import ./renderers/docent_fxtr_proxy/fxtr_renderer.nix {
          inherit lib dqlResultType dqlSchemaType metadataFieldsType keyIds;
        })
        // (import ./renderers/delayed_echo/fxtr_renderer.nix { inherit lib delayedEchoType keyIds; })
        // (import ./renderers/demo_helper/fxtr_renderer.nix { inherit lib demoHelperType keyIds; })
        // (import ./renderers/simple_scheduler/fxtr_renderer.nix {
          inherit lib schedulerType schedulerRunTagType keyIds;
        })
        // (import ./renderers/streaming_scheduler/fxtr_renderer.nix {
          inherit lib streamingSchedulerType streamingRunTagType taskProgressTagType keyIds;
        })
        // (import ./renderers/simple_run_result/fxtr_renderer.nix { inherit lib runResultType keyIds; })
        // (import ./renderers/note_with_links/fxtr_renderer.nix {
          inherit lib noteWithLinksType promptTemplateType keyIds;
        });
    in
    {
      # Sandboxed service (CODE, git-ref spawn). The agent spawns it via
      #   NixWasmServiceDefn(git_commit=current_commit(),
      #                      flake_attr="fxtr.services.delayed_echo")
      # `src` is the sandbox_spawn/ subfolder — the wasm spawn-env
      # declaration (entry point + wasi-pinned deps); the host-importable
      # code package is its parent dir.
      fxtr.services.delayed_echo = lib.mkWasmService {
        src = ./packages/delayed_echo/sandbox_spawn;
        name = "fxtr-delayed-echo";
      };

      # --- simple workflow demo (scheduler + tasks) ---
      # Basic SimpleTask transforms; the connect config (a string) picks
      # the task, multi-connect on one spawn pulls out several tasks.
      fxtr.services.simple_task_lib = lib.mkWasmService {
        src = ./packages/simple_task_lib;
        name = "fxtr-simple-task-lib";
      };
      # SimpleTask: Note -> Note through an LLM ask-service, prompt
      # template supplied at connect time
      # ({"llm": ref, "prompt_template": PromptTemplate FxtrID}).
      fxtr.services.prompt_task = lib.mkWasmService {
        src = ./packages/prompt_task;
        name = "fxtr-prompt-task";
      };
      # The sequential-chain batch scheduler (split shape — `src` is the
      # sandbox_spawn/ wasm env; the impl lives in the parent package).
      fxtr.services.simple_scheduler = lib.mkWasmService {
        src = ./packages/simple_scheduler/sandbox_spawn;
        name = "fxtr-simple-scheduler";
      };

      # --- streaming workflow demo (nested channels, stage 13 part 4) ---
      # Streaming tasks (connect config picks the task; currently
      # "word_shout").
      fxtr.services.streaming_task_lib = lib.mkWasmService {
        src = ./packages/streaming_task_lib;
        name = "fxtr-streaming-task-lib";
      };
      # The nested-channel scheduler: per-cell progress channels written
      # by the tasks through handed-in sinks (split shape, like
      # simple_scheduler).
      fxtr.services.streaming_scheduler = lib.mkWasmService {
        src = ./packages/streaming_scheduler/sandbox_spawn;
        name = "fxtr-streaming-scheduler";
      };

      # Claude Haiku chat service, wasm flavor (CODE, git-ref spawn).
      # Network access comes from connecting it to the workstation's
      # `anthropic-authorizer`; raw httpx, no SDK (native deps can't go
      # through the pure-python wasm lock). Its container sibling
      # (haiku_service_oci) uses the real SDK.
      fxtr.services.haiku_service_wasm = lib.mkWasmService {
        src = ./packages/haiku_service_wasm;
        name = "fxtr-haiku-service-wasm";
      };

      # Claude Haiku chat service, container/OCI flavor (CODE, spawned by
      # git ref via NixContainerServiceDefn). A nix2container image running
      # real Linux CPython, so the actual `anthropic` SDK (native wheels) is
      # a normal dependency; network still flows through the connect-time
      # authorizer tunnel.
      fxtr.services.haiku_service_oci = lib.mkContainerService {
        src = ./packages/haiku_service_oci;
        name = "fxtr-haiku-service-oci";
      };

      # Streaming LLM prompt task, container/OCI flavor (CODE, spawned by git
      # ref via NixContainerServiceDefn). A StreamingTaskProto that streams a
      # Claude reply through the streaming scheduler's per-cell sink; OCI so
      # the real `anthropic` SDK's native streaming handles the SSE wire.
      fxtr.services.streaming_prompt_task_oci = lib.mkContainerService {
        src = ./packages/streaming_prompt_task_oci;
        name = "fxtr-streaming-prompt-task-oci";
      };

      # DATA entity (built on demand by the MCP's retrieve_nix_entity, from the
      # live workdir). Shares the aggregator venv (presentables provides
      # DEMO_NOTE).  await retrieve_nix_entity("demo_note")
      fxtr.entities.demo_note = lib.mkEntityFromPython {
        venv = typeEntitiesVenv;
        fromPython = "presentables:DEMO_NOTE";
        layerKey = "demo_note";
      };

      # UI preset (CODE, referenced by git commit). Auto-loaded on boot by the
      # demo's NixConfigurableDispatchImpl, and re-pushed by the agent after
      # editing a renderer/the flake:
      #   schedule_invoke("p", dispatcher, lambda d: d.set_preset(
      #       current_commit(), "fxtr.uiPresets.default"))
      fxtr.uiPresets.default = lib.mkUiPreset rendererEntries;
    };
}
