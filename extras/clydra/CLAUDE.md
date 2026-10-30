The clydra directory is self-contained; you shouldn't have to worry about the code outside this directory unless told specifically.

You can read the files in the `specs` directory for high-level context about what we are building.

For an overview of the contents of the prototype, see CODE_OVERVIEW.md in this folder. You can then use this to explore the parts of the codebase that are relevant; I suggest starting with the specs/overview.md and then reading the plans relevant to your task.

Whenever you make a substantial change to the codebase organization (e.g. adding a new file, or refactoring the fundamental structure of something), please also modify CODE_OVERVIEW.md so that it is up to date! (You do not need to write a chronological log of everything you changed here, this doc should instead describe the _current_ state of the codebase, for someone coming to it with fresh eyes. You should make sure that the file listing is still accurate, that any new fundamental concepts are briefly mentioned, and that generally if someone was asked to make a change that they could use this file to figure out what to read and what to change.) Prefer modifying CODE_OVERVIEW as you go, whenever you finish a subtask that is worth tracking there, instead of doing it all at the end. (For instance, you should update CODE_OVERVIEW for a new feature before you start writing tests.)
ø
Plans go in `plans`. When writing plans, treat the plan like a high-level spec for how to implement the given task. You can ask clarification questions if you have them, using either your ask-questions tool or free-form questions over chat, and then finally you can write a consise plan about how to implement the feature. When writing a plan, code blocks should be small, and should focus on explaining the moving parts, interface boundaries, design decisions, and what would need to be done to implement it rather than including actual snippets of the implementation. Pseudocode is OK. Write it as if you expect someone new to read it to understand what you are planning to do, or to refer back to it during code review once you have a real implementation. Always put "Update CODE_OVERVIEW.md" as the last step in your list of implementation steps; you can also put intermediate "Update CODE_OVERVIEW.md" steps after major sub-features. If there's a LOCAL_CODE_OVERVIEW for the part of the project you're touching, you should update that too.

When creating a plan, you may want to think through the details first. To avoid cluttering the plan, you should create a new file `{name_of_plan}_scratch.md` adjacent to the original plan doc, and write your scratch thoughts there.

When implementing a plan, please check off your progress in the plan doc as you go. The plan doc should always capture any units of work that you've completed. This means _whenever_ you finish a step, always check it off in the plan _before_ you start on the next step. (Exception: if you can complete multiple consecutive todos at once, you can do them all and then check them off. But never move onto a new file / new section of the plan without checking off the task first.)

When you finish with a plan, in your message back to me, always conclude by noting any weird things you ran into during implementation or things that we might need to revisit later. I may ask you to add those to the plan. (If they are things you think we definitely shouldn't lose track of, you can go ahead and add them to the bottom of the plan first.)

Default to using `anyio`'s APIs for async instead of `asyncio`.

When launching servers in the background, _always_ start the server in a separate bash tool call in background mode. Don't use `&` to background a command, this is error prone and makes it difficult to control the background process later.

Do not try to guess version numbers! If you are not sure the latest version of an npm package, use `npm view` or similar to get the latest version.
