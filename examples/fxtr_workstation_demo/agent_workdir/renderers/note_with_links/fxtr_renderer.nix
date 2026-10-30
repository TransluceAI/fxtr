# NoteWithLinks + PromptTemplate renderer slice for the demo's `default`
# UI preset (both types from packages/simple_tasks/). One module, two
# subtype entries. Base externals suffice.
{ lib, noteWithLinksType, promptTemplateType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
  allKeys = [
    fullPanel
    blockLink
    inlineLink
    hoverPreview
  ];
in
{
  note_with_links = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes = {
      note_with_links = {
        forType = noteWithLinksType;
        forDispatchKeys = allKeys;
      };
      prompt_template = {
        forType = promptTemplateType;
        forDispatchKeys = allKeys;
      };
    };
  };
}
