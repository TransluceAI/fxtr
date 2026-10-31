## Intent files

The purpose of an intent file is to describe key decisions that have been intentionally made by human designers and engineers (as opposed to coding agents).

An intent file is a file called `INTENT.md`. If it gets too long, it can be split up into `intent/*.md` (keep an overview in `intent/index.md)

If you're working in a directory with an intent file, keep it up to date. If you're working in a directory without an intent file, do not create one.

Purpose of intent files:
* An engineer can answer a question such as "did my colleague mean to do this, or did Claude add it by accident?"
* When reviewing a large agent-written PR, humans can focus on reviewing the intent rather than the implementation details
* An engineer can make extended changes to the intent file and ask an agent to "sync with intent" to update the implementation in one go, rather than one prompt at a time
* If the user requests a change that goes against a non-obvious UX principle in the intent file, you can flag it and ensure changes are made deliberately rather than "tearing down Chersterton's fence"

The intent file is not a notepad for coding agents. Never add your (the agent's) own ideas, design decisions, or interpretations to the intent file. The intent file is not a description of the code as it exists. It must only contain explicit, deliberate decisions by the user. Some sessions may involve extensive code changes without changes to the intent file.

* User says "try making the progress bar gray" -> inconsequential change, doesn't go in intent file
* User says "the progress bar should be gray because blue is reserved for selection" -> "reserve blue for selection" is an intentional decision, update intent file
* User approves an agent-authored implementation plan -> no update to intent file. The content of the plan originated from an agent, not human intent

Generally, make minimal changes to the intent file so the diff is easy to review. When adding new things, articulate them clearly and concisely (even if the user rambled) and add them to the appropriate place (creating new sections as needed, but sparingly). If the user was ambiguous about something which belongs in an intent file, resolve any ambiguities with the user.

If the user asks you to "refactor intent files", you can make larger changes to ensure clarity and organization. Do not add or change content when refactoring intent files. Maintain the invariant that intent file content was provided explicitly by the user.
