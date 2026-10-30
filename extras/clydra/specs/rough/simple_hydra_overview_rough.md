# Clydra = Claude Code Hydra

coding agents like claude code have difficulty dealing with very large codebases. they spend a lot of time reading files and re-familiarizing themselves with what is going on.

part of the problem is that we expect coding agents to be able to do large-scale edits in a single session while holding _all_ of what has happened so far in the context, e.g. in short-term memory. but this is a limited buffer! eventually we run out of space and then have to clear the context, but compaction is often lossy and misses things that were actually important.

this project attempts to solve this by allowing _selective short-term memory_ via _agent-driven timeline forking_. the idea is that, instead of treating the context as a single growing list, we treat it like a depth-first-search tree. as information becomes no-longer-important, the agent can choose to "pop" those specific memories, and replace them with a summary. it is agent-driven so that the coding agent is not artificially constrained by the system; the agent can choose what memories are important to keep. this is also efficient due to prompt caching; we can efficiently drop new memories from the _end_ of the token stream without messing up earlier cached messages.

additionally, we make it possible for the agent to do _cross-branch communication_ in order to revisit old contexts, and exchange messages between them. this means that, if some important detail was missed when e.g. a file was written, we can figure out what the original reasoning was.

we specifically structure the agent transcript as a tree of nested memory regions, with the trajectory being a sort of depth-first-search. each memory region has a unique identifier. while in a memory region, the agent has full memory of anything that happened in the region or any parent region, but compressed memories of any region that is not a parent. new regions can be opened to track new subtasks or to scout out info. regions can be closed whenever something is done and a detailed memory is no longer needed.

## Agent-facing tools and capabilities

Conceptually, we take a coding harness like Claude Code and add a few special tools, which break out of the single timeline and allow more advanced behaviors.

**MemoryRegionBegin tool:** Starts a new memory region with a unique ID, nested inside the current one, with a short description. Later, if the assistant wants, it can clear its memories inside this region and replace them with a summary. This is a low-level primitive, and can be used by the agent right before it starts something that is logically a new task or subtask, or which might produce a lot of output that isn't very useful that it could choose to forget about later.

When the agent uses the MemoryRegionBegin tool, it may immediately receive a compressed summary of whatever it was about to do, stating that it has already been done. This happens if the agent decided to restore itself back to this checkpoint later, using the `MemoryRegionPop` tool.

**MemoryRegionHindsightBegin tool:** Retroactively mark some previous event as the start of a memory region with a unique ID. This can be used to start a region around a large tool call if it turns out that the tool produced more output than was expected. This tool takes a description and also a hindsight-checkpoint ID; hindsight checkpoints are created automatically by the system at various points and sent to the agent as hook outputs.

Guideline: use this if we expect that less than 3/4 of the content of the previous tool will be useful for accomplishing the goal of the current memory region. In this case, we may want to immediately do a **MemoryRegionPop**, or do a few more tool calls and then pop. Unnecessary if we just did a **MemoryRegionBegin** before calling the tool(s).

**MemoryRegionCommit tool:** Exit a memory region and commit the _entire_ region to short-term memory. This means the agent will continue to remember everything about this region, but the region is no longer active.

Guideline: use this if it is not practical to quote out or summarize the important findings from this memory region, and we expect that more than 3/4 of the content of this memory region would be directly useful for accomplishing the goal of the parent region. Use sparingly.

**MemoryRegionPop tool:** Exit a memory region, save the current state of memory to a suspended branch, and pop back out to an earlier state with an identical prefix but a compressed summary of the region.
- The agent, with full memory of everything that has happened so far, picks one of its own memory regions, and invokes MemoryRegionPop.
- The agent passes back a summary of what happened, along with a sequence of "quotes": small snippets of previous tool call results or other messages that the agent wants to preserve verbatim.
- The agent's full memories in this branch are archived.
- The agent's memory is restored to the state when it initially called MemoryRegionBegin (or, to the state later labeled with MemoryRegionHindsightBegin). It then receives the summary message and quotes. It also receives a special "branch ID" for the archived memories.
- The agent can now continue its original task in the newly-active branch, without having to worry about any unnecessary information or detours involved in the archived branch.

Guideline: use this anytime less than 3/4 of the information in short-term memory is not relevant to the parent region. Be very eager to pop memories from reading files or writing files as soon as you are logically moving on to a new (sub)task. Also ensure to pop memories immediately if you can immediately tell that some of the tool result is garbage or irrelevant. (You can chunk things as needed, e.g. by reading a few files, identifying the important pattern, and then popping all of these reads, rather than popping each one at a time.) (Note that if you read or write a file and then spend a bunch of time editing it, it might make sense to pop this memory and then re-read the final state, or re-read the final state before popping and then quote speicific pieces from it, instead of remembering all the edits.)

**MemoryRecall tool:** Temporarily re-enter a memory branch that was archived by MemoryRegionPop and communicate with it.
- The agent calls MemoryRecall with a particular memory branch ID, passing a message and optionally quoting parts of its own short-term memory.
- The system re-hydrates the suspended MemoryRegion with that branch ID, and sends the message to that version of the agent. That version of the agent also receives an ID for a new "recall" memory region, which represents being asked this question.
- That version of the agent can respond based on its memory. (It can also, as needed, call **MemoryRecall** itself, for memories it has already forgotten.)
- Eventually the suspended agent can return its answer back to the original agent, along with a new set of quotes, using **MemoryRegionPop** to exit the recall region. (It is not allowed to pop beyond this region; all other regions are locked at this point.)
- This creates a _new_ suspended memory branch ID, which remembers being asked this question and answering; the original memory branch ID can also be used if the memory of being asked this question is not important.

Suggestion: Refer to the shared self across branches as "we", since the two branches have some common memories but also different current states. (But also you can still use "I" to specifically refer to the current, distinct state of the communicating agent. Or just use whatever feels natural.)


## Implementation core idea

Memory regions are not natively supported by claude code or the claude agent sdk, which clydra is built on. However, the claude agent sdk supports resuming from a transcript file, and stores all of its state inside that transcript file. So, we can manipulate the history by editing the transcript file, or by forging new transcript files with new histories.

To implement MemoryRegionBegin or MemoryRegionHindsightBegin: We just need a way to identify which message is at the start of the region, and save it into an auxiliary store. For MemoryRegionHindsightBegin to work, we need to also have a post-tool-use hook that saves hindsight data and informs the agent about it.

To implement MemoryRegionPop:
- Use a post-tool-use hook to immediately stop the agent after MemoryRegionPop is called
- End the claude agent sdk session
- Archive the transcript outside of claude code
- Edit the suffix of the transcript to remove everything inside the memory region.
- Recreate the claude agent sdk session with the same session ID. It will now no longer have those messages in it.
- Send a new "automated" message to the agent containing the results from MemoryRegionPop

To implement MemoryRecall:
- Find the archived transcript. Save it to a new UUID, changing all of its session ID parameters to refer to the new UUID. (This is what claude-agent-sdk does internally when it forks a session.)
- Create a new background task
- In that task, create a new claude agent sdk session, and have it resume the archived session. Set up the metadata for it so that it is also running clydra and has these tools, but attached to that new session.
- Wait until it pops the recall region
- Send the result back to the original session

## Implementation details
/Users/daniel/prototypes/scratch1/clydra/scratchwork/hindsight_rewind/experiment_report.md has some preliminary experiments that demonstrate the feasibility of the approach.
