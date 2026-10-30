# Phase 0 Step 3d: AskUserQuestion Support

## Features

1. **AskUserQuestion handling** - Detect and handle `AskUserQuestion` tool calls via `canUseTool` callback
2. **Question dialog UI** - Tab-based question dialog with option selection and free-text support
3. **Multiline input** - Both question free-text and main user input as multiline textareas

## Problem Details

### Current state
- `canUseTool` callback only handles tool permission requests
- No detection of `AskUserQuestion` tool (Claude's clarifying question mechanism)
- Permission dialog only shows allow/deny for tools
- Main user input is single-line `<input type="text">`

### Desired state
- `canUseTool` detects `tool_name == "AskUserQuestion"` and routes to question handler
- Reuse `PermissionDialogController` with a second mode for questions
- Tab-based question UI: up/down for options, left/right for tabs, last tab is summary with Enter to submit
- Every question shows "Other" option with multiline textarea for free response
- Main user input is also multiline textarea with word wrap

## SDK AskUserQuestion Format

**Input structure:**
```python
{
  "questions": [
    {
      "question": "How should I format the output?",  # full question text
      "header": "Format",                             # short label (max 12 chars)
      "options": [
        { "label": "Summary", "description": "Brief overview" },
        { "label": "Detailed", "description": "Full explanation" }
      ],
      "multiSelect": false  # whether multiple options can be selected
    }
  ]
}
```

**Response format:**
```python
PermissionResultAllow(
  updated_input={
    "questions": [...],  # pass through original questions
    "answers": {
      "How should I format the output?": "Summary",  # question -> selected label(s)
      "Which sections?": "Intro, Conclusion"         # multi-select: comma-joined
    }
  }
)
```

For free-text, use the user's custom text as the value (not "Other").

## Implementation

### 1. Extend PermissionDialogController

**permission_dialog.py:**

Add new dataclass for question requests:
```python
@dataclass
class QuestionOption:
    label: str
    description: str

@dataclass
class Question:
    question: str
    header: str
    options: list[QuestionOption]
    multi_select: bool

@dataclass
class QuestionRequest:
    request_id: str  # uuid4() for state reset detection
    questions: list[Question]
```

Update existing `PermissionRequest` to also have `request_id`:
```python
@dataclass
class PermissionRequest:
    request_id: str  # uuid4() for state reset detection
    tool_name: str
    tool_input: dict[str, Any]
    suggestions: list[dict[str, Any]]
```

Change controller to use a single union field:
```python
@dataclass
class PermissionDialogController:
    # Single field, discriminated by type
    pending_request: PermissionRequest | QuestionRequest | None = None
```

Add `request_id` to both request types (generated via `uuid.uuid4()`) for frontend state reset detection.

Update snapshotter to detect type and include appropriate data:
```python
def stateless_snapshot(self, controller):
    pending_data = None
    if isinstance(controller.pending_request, PermissionRequest):
        pending_data = {
            "type": "permission",
            "request_id": controller.pending_request.request_id,
            "tool_name": ...,
            "tool_input": ...,
            "suggestions": ...,
        }
    elif isinstance(controller.pending_request, QuestionRequest):
        pending_data = {
            "type": "question",
            "request_id": controller.pending_request.request_id,
            "questions": [...],
        }

    return ThingState(..., thing_data={"pending_request": pending_data}, ...)
```

### 2. Modify canUseTool callback

**main.py - create_can_use_tool_callback:**

Detect `AskUserQuestion` and handle differently:
```python
async def can_use_tool(tool_name, input_data, context):
    if tool_name == "AskUserQuestion":
        # Parse questions from input_data
        questions = parse_questions(input_data.get("questions", []))
        dialog_ctrl.pending_request = QuestionRequest(questions=questions)
        watcher.streaming_update(dialog_ctrl)

        # Block until user responds
        payload = await watcher.receive(dialog_ctrl)

        # Clear pending request
        dialog_ctrl.pending_request = None
        watcher.streaming_update(dialog_ctrl)

        # Build response with answers mapping
        return PermissionResultAllow(
            updated_input={
                "questions": input_data.get("questions", []),
                "answers": payload.get("answers", {}),
            }
        )

    # ... existing permission dialog handling (also uses pending_request) ...
```

### 3. Update PermissionDialog.tsx for questions

**Updated types (single discriminated union):**
```tsx
interface QuestionOption {
  label: string;
  description: string;
}

interface Question {
  question: string;
  header: string;
  options: QuestionOption[];
  multiSelect: boolean;
}

// Discriminated union for pending_request
interface PendingPermissionRequest {
  type: "permission";
  request_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  suggestions: PermissionSuggestion[];
}

interface PendingQuestionRequest {
  type: "question";
  request_id: string;
  questions: Question[];
}

type PendingRequest = PendingPermissionRequest | PendingQuestionRequest;

interface ChunkData {
  pending_request?: PendingRequest | null;
}
```

**Tab-based question UI:**

State (using `usePersistentThingState` for persistence across re-mounts):
```tsx
// For single-select: answers[questionIndex] = optionIndex (number)
// For multi-select: answers[questionIndex] = [optionIdx1, optionIdx2, ...] (number[])
// optionIndex === options.length means "Other" (free text) is selected
// Type: (number | number[])[] to handle both cases
const [answers, setAnswers] = usePersistentThingState<(number | number[])[]>(handle, "answers");
const [freeText, setFreeText] = usePersistentThingState<string[]>(handle, "freeText");
const [currentTab, setCurrentTab] = usePersistentThingState<number>(handle, "currentTab");
const [focusedOption, setFocusedOption] = usePersistentThingState<number>(handle, "focusedOption");
const [lastRequestId, setLastRequestId] = usePersistentThingState<string>(handle, "lastRequestId");
```

**State reset on new request:**
- Compare `pending_request.request_id` to `lastRequestId`
- If different: reset all state (answers, freeText, currentTab, focusedOption) and update `lastRequestId`
- Also clear state after successful submit

Rendering:
1. Tab bar at top showing question headers + "Submit" tab
2. Current tab content:
   - If question tab: show question text, options as selectable buttons (selected = answers[tabIdx]), "Other" with textarea
   - If submit tab: show all questions with selected answers (resolved to labels or free text), submit button
3. Keyboard navigation:
   - Left/Right: switch tabs
   - Up/Down: move between options in current question (including "Other" as last option)
   - Enter on submit tab: submit
   - When "Other" is focused and user types, select it automatically

Option selection:
- Single-select: clicking option sets `answers[tabIdx] = optionIdx`
- Multi-select: clicking option toggles that index in the array
- "Other" is **mutually exclusive** even in multi-select: selecting "Other" clears other selections, selecting any other option clears "Other"
- "Other" selected when `answers[tabIdx] === options.length` (single) or `answers[tabIdx].includes(options.length)` (multi)

**Submit tab:**
- Shows summary of all questions with their selected answers
- Warn (yellow highlight) for any unanswered questions - but don't block submit
- Submit button (Enter key)

**Abort button (both modes):**
- Add "Abort" button to both permission and question dialogs
- Triggers `{ action: "abort" }` which backend converts to `PermissionResultDeny(interrupt=True)`
- Esc key should also trigger abort (update existing Esc handler in permission mode)

**Converting index-based answers to SDK format:**
```tsx
function buildAnswersPayload(
  questions: Question[],
  answers: (number | number[])[],
  freeText: string[]
): Record<string, string> {
  const result: Record<string, string> = {};
  questions.forEach((q, qIdx) => {
    const selected = answers[qIdx];
    if (selected === undefined || (Array.isArray(selected) && selected.length === 0)) {
      // Unanswered - use empty string
      result[q.question] = "";
    } else if (q.multiSelect) {
      // Multi-select: answers[qIdx] is array of indices
      // Note: "Other" is mutually exclusive, so if it's selected, it's the only one
      const indices = selected as number[];
      if (indices.includes(q.options.length)) {
        result[q.question] = freeText[qIdx] || "";
      } else {
        const labels = indices.map(i => q.options[i].label);
        result[q.question] = labels.join(", ");
      }
    } else {
      // Single-select: answers[qIdx] is a single index
      const idx = selected as number;
      if (idx === q.options.length) {
        result[q.question] = freeText[qIdx] || "";
      } else {
        result[q.question] = q.options[idx].label;
      }
    }
  });
  return result;
}
```

### 4. Convert inputs to multiline textareas

**PermissionDialog.tsx - "Other" input:**
```tsx
<textarea
  value={freeTextInput[currentQuestion.question] || ""}
  onChange={(e) => handleFreeTextChange(currentQuestion.question, e.target.value)}
  placeholder="Type your own answer..."
  style={{
    ...styles.freeTextArea,
    resize: "none",
    minHeight: "60px",
    whiteSpace: "pre-wrap",
    wordWrap: "break-word",
  }}
/>
```

**ClydraRoot.tsx - main user input:**
```tsx
<textarea
  value={inputText}
  onChange={(e) => setInputText(e.target.value)}
  onKeyDown={handleKeyDown}  // Shift+Enter for newline, Enter for send
  disabled={!triggerable}
  placeholder={...}
  style={{
    ...styles.input,
    resize: "none",
    minHeight: "40px",
    maxHeight: "200px",
    whiteSpace: "pre-wrap",
    wordWrap: "break-word",
  }}
  rows={1}
/>
```

Update `handleKeyDown`:
- Enter (without Shift): send message
- Shift+Enter: insert newline

## Files to Change

1. **clydra/permission_dialog.py** - Add `QuestionRequest` dataclass, use single union field on controller, update snapshotter with type discriminator
2. **clydra/main.py** - Update `can_use_tool` callback to detect/handle `AskUserQuestion`
3. **clydra/renderers/PermissionDialog.tsx** - Add question dialog mode with tabs, options, free-text, use `usePersistentThingState`
4. **clydra/renderers/ClydraRoot.tsx** - Convert main input to multiline textarea

## Implementation Order

1. [x] Add `QuestionRequest` and related types to permission_dialog.py (include `request_id: str` on both request types)
2. [x] Change `PermissionDialogController.pending_request` to `PermissionRequest | QuestionRequest | None`
3. [x] Update `PermissionDialogControllerSnapshotter` with type discriminator in output
4. [x] Update `can_use_tool` callback in main.py for AskUserQuestion detection
5. [x] Update `can_use_tool` to handle `action: "abort"` → `PermissionResultDeny(interrupt=True)`
6. [x] Add question types to PermissionDialog.tsx
7. [x] Implement state reset logic using `request_id` comparison
8. [x] Implement tab-based question UI with `usePersistentThingState` for answers/currentTab/freeText
9. [x] Implement option selection (single/multi) with keyboard navigation
10. [x] Implement "Other" free-text textarea (mutually exclusive with other options)
11. [x] Implement submit tab with answer summary + unanswered warnings
12. [x] Add "Abort" button to both permission and question modes
13. [x] Update Esc key handler to trigger abort (with interrupt)
14. [x] Convert ClydraRoot.tsx input to multiline textarea
15. [x] Update handleKeyDown for Shift+Enter newline behavior
16. [x] Update CODE_OVERVIEW.md

## Design Decisions

- **Single union field**: `pending_request: PermissionRequest | QuestionRequest | None` with type discriminator. Simpler than two separate fields, frontend checks `pending_request.type` to determine render mode.
- **Request ID for state reset**: Each request gets a `uuid4()` ID. Frontend compares to `lastRequestId` to detect new requests and reset state. Cleared after submit.
- **Index-based answers state**: `answers[questionIndex] = optionIndex` where `optionIndex === options.length` means free text. Simpler than string-keyed maps, direct array access.
- **usePersistentThingState**: State persists across re-mounts (e.g., if user switches views). Keyed by thing shortId + state key.
- **"Other" mutually exclusive**: Even in multi-select, selecting "Other" clears other selections and vice versa. Simplifies the answer format (no mixing predefined + custom).
- **Tab-based multi-question**: Rather than stacking all questions vertically, tabs keep the UI compact and focused on one question at a time. Keep tabs even for single question for consistency.
- **Submit tab with warnings**: Last tab shows all answers for review. Unanswered questions show yellow warning but don't block submit. Enter key submits.
- **Abort button**: Both permission and question dialogs have "Abort" button that sends `PermissionResultDeny(interrupt=True)`. Esc key also triggers abort.
- **Keyboard navigation**: Left/Right for tabs matches horizontal tab layout. Up/Down for options matches vertical option list. Familiar navigation pattern.
- **Always show "Other"**: SDK docs recommend supporting free-text for every question since predefined options won't always match user intent.
- **Multiline textareas**: Free responses can be detailed. Main input textarea enables multi-line code snippets or longer instructions.
- **Shift+Enter for newlines**: Standard pattern in chat interfaces (Discord, Slack, etc.).
