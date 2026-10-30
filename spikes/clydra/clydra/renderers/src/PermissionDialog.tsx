// PermissionDialog.tsx - Permission and question dialog renderer for Clydra
// Typecheck with: deno check PermissionDialog.tsx
import { useState, useEffect, useRef } from "react";
import type {
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  RendererSet,
} from "thingwatcher-bindings";
import {
  useDataChunks,
  useTriggerable,
  useTrigger,
  usePersistentThingState,
} from "thingwatcher-bindings";
import { WriteContentView, EditDiffView } from "./formatting.tsx";

export const thingwatcherDialect = "0.1";

// =============================================================================
// Types
// =============================================================================

interface PermissionSuggestion {
  type: string;
  behavior?: string;
  mode?: string;
  rules?: Array<{ tool_name: string; rule_content?: string }>;
  [key: string]: unknown;
}

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

interface PermissionOption {
  type: "allow" | "deny" | "abort" | "suggestion";
  label: string;
  suggestion?: PermissionSuggestion;
}

// =============================================================================
// Styles
// =============================================================================

const styles = {
  container: {
    background: "#1a1a2a",
    border: "1px solid #4a4a6a",
    padding: "12px",
    fontFamily: "monospace",
    fontSize: "12px",
  },
  header: {
    color: "#e0e0e0",
    marginBottom: "8px",
    fontWeight: "bold" as const,
  },
  toolName: {
    color: "#8be9fd",
  },
  inputSection: {
    marginBottom: "12px",
  },
  summary: {
    cursor: "pointer",
    color: "#888",
    fontSize: "11px",
  },
  codeBlock: {
    background: "#111",
    border: "1px solid #333",
    padding: "8px",
    overflow: "auto",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
    fontSize: "11px",
    marginTop: "4px",
    maxHeight: "200px",
  },
  optionsContainer: {
    display: "flex",
    flexDirection: "column" as const,
    gap: "6px",
    marginBottom: "8px",
  },
  optionRow: {
    display: "flex",
    alignItems: "flex-start",
    gap: "8px",
  },
  button: {
    padding: "4px 8px",
    border: "1px solid #333",
    cursor: "pointer",
    color: "#e0e0e0",
    background: "#222",
    fontSize: "11px",
    minWidth: "60px",
    flexShrink: 0,
  },
  buttonFocused: {
    background: "#2a4a6a",
    borderColor: "#4a90d9",
  },
  buttonSelected: {
    background: "#2a6a4a",
    borderColor: "#4ad990",
  },
  buttonFocusedSelected: {
    background: "#2a6a4a",
    borderColor: "#4a90d9",
    outline: "1px solid #4a90d9",
    outlineOffset: "1px",
  },
  buttonAbort: {
    background: "#4a2222",
    borderColor: "#d94a4a",
  },
  buttonAbortFocused: {
    background: "#4a2222",
    borderColor: "#4a90d9",
    outline: "1px solid #4a90d9",
    outlineOffset: "1px",
  },
  suggestionJson: {
    background: "#111",
    border: "1px solid #333",
    padding: "4px 8px",
    fontSize: "10px",
    color: "#888",
    overflow: "auto",
    whiteSpace: "pre" as const,
    flex: 1,
    maxHeight: "80px",
  },
  hint: {
    color: "#666",
    fontSize: "10px",
  },
  // Question mode styles
  tabBar: {
    display: "flex",
    gap: "4px",
    marginBottom: "12px",
    flexWrap: "wrap" as const,
  },
  tab: {
    padding: "4px 8px",
    border: "1px solid #333",
    background: "#222",
    color: "#888",
    cursor: "pointer",
    fontSize: "11px",
  },
  tabActive: {
    background: "#2a4a6a",
    borderColor: "#4a90d9",
    color: "#e0e0e0",
  },
  questionText: {
    color: "#e0e0e0",
    marginBottom: "8px",
    fontSize: "12px",
  },
  optionButton: {
    padding: "6px 10px",
    border: "1px solid #333",
    background: "#222",
    color: "#e0e0e0",
    cursor: "pointer",
    fontSize: "11px",
    textAlign: "left" as const,
    width: "100%",
  },
  optionDescription: {
    color: "#888",
    fontSize: "10px",
    marginLeft: "8px",
  },
  freeTextArea: {
    width: "100%",
    minHeight: "60px",
    padding: "8px",
    border: "1px solid #333",
    background: "#111",
    color: "#e0e0e0",
    fontFamily: "monospace",
    fontSize: "11px",
    resize: "none" as const,
    whiteSpace: "pre-wrap" as const,
    wordWrap: "break-word" as const,
    boxSizing: "border-box" as const,
  },
  summaryItem: {
    marginBottom: "8px",
    padding: "6px",
    background: "#222",
    border: "1px solid #333",
  },
  summaryQuestion: {
    color: "#888",
    fontSize: "10px",
    marginBottom: "4px",
  },
  summaryAnswer: {
    color: "#e0e0e0",
    fontSize: "11px",
  },
  warningText: {
    color: "#d9a54a",
    fontStyle: "italic" as const,
  },
  submitButton: {
    padding: "8px 16px",
    border: "1px solid #4a90d9",
    background: "#2a4a6a",
    color: "#e0e0e0",
    cursor: "pointer",
    fontSize: "12px",
    marginTop: "8px",
  },
};

// =============================================================================
// Permission Mode Component
// =============================================================================

function PermissionModeRenderer({
  request,
  triggerable,
  trigger,
}: {
  request: PendingPermissionRequest;
  triggerable: boolean;
  trigger: (payload: unknown) => Promise<unknown>;
}) {
  const [focusedIndex, setFocusedIndex] = useState(0);

  // Build options list with Abort
  const options: PermissionOption[] = [
    { type: "allow", label: "Allow" },
    { type: "deny", label: "Deny" },
    ...request.suggestions.map((s, i) => ({
      type: "suggestion" as const,
      label: `Apply ${i + 1}`,
      suggestion: s,
    })),
    { type: "abort", label: "Abort" },
  ];

  // Reset focus when request changes
  useEffect(() => {
    setFocusedIndex(0);
  }, [request.request_id]);

  // Keyboard navigation
  useEffect(() => {
    if (!triggerable) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      switch (e.key) {
        case "ArrowUp":
          e.preventDefault();
          setFocusedIndex((i) => Math.max(0, i - 1));
          break;
        case "ArrowDown":
          e.preventDefault();
          setFocusedIndex((i) => Math.min(options.length - 1, i + 1));
          break;
        case "Enter":
          e.preventDefault();
          if (options[focusedIndex]) {
            selectOption(options[focusedIndex]);
          }
          break;
        case "Escape":
          e.preventDefault();
          trigger({ action: "abort" });
          break;
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [focusedIndex, options.length, triggerable, request.request_id]);

  const selectOption = async (option: PermissionOption) => {
    if (!triggerable) return;

    if (option.type === "allow") {
      await trigger({ action: "allow" });
    } else if (option.type === "deny") {
      await trigger({ action: "deny" });
    } else if (option.type === "abort") {
      await trigger({ action: "abort" });
    } else if (option.type === "suggestion" && option.suggestion) {
      await trigger({ action: "allow", updated_permissions: [option.suggestion] });
    }
  };

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        Permission Request:{" "}
        <span style={styles.toolName}>{request.tool_name}</span>
      </div>

      <details open style={styles.inputSection}>
        <summary style={styles.summary}>Input</summary>
        {request.tool_name === "Write" ? (
          <WriteContentView
            input={request.tool_input}
            rawView={
              <pre style={styles.codeBlock}>
                {JSON.stringify(request.tool_input, null, 2)}
              </pre>
            }
          />
        ) : request.tool_name === "Edit" ? (
          <EditDiffView
            input={request.tool_input}
            rawView={
              <pre style={styles.codeBlock}>
                {JSON.stringify(request.tool_input, null, 2)}
              </pre>
            }
          />
        ) : (
          <pre style={styles.codeBlock}>
            {JSON.stringify(request.tool_input, null, 2)}
          </pre>
        )}
      </details>

      <div style={styles.optionsContainer}>
        {options.map((option, i) => (
          <div key={i} style={styles.optionRow}>
            <button
              type="button"
              style={{
                ...styles.button,
                ...(i === focusedIndex ? styles.buttonFocused : {}),
                ...(option.type === "abort" ? styles.buttonAbort : {}),
              }}
              onClick={() => selectOption(option)}
              onMouseEnter={() => setFocusedIndex(i)}
            >
              {option.label}
            </button>
            {option.suggestion && (
              <pre style={styles.suggestionJson}>
                {JSON.stringify(option.suggestion, null, 2)}
              </pre>
            )}
          </div>
        ))}
      </div>

      <div style={styles.hint}>
        {"\u2191\u2193"} navigate, Enter select, Esc abort
      </div>
    </div>
  );
}

// =============================================================================
// Question Mode Component
// =============================================================================

function QuestionModeRenderer({
  request,
  triggerable,
  trigger,
  handle,
}: {
  request: PendingQuestionRequest;
  triggerable: boolean;
  trigger: (payload: unknown) => Promise<unknown>;
  handle: FullRendererProps["handle"];
}) {
  const questions = request.questions;
  const numTabs = questions.length + 1; // questions + submit tab

  // Persistent state
  const [answers, setAnswers] = usePersistentThingState<(number | number[])[]>(handle, "answers");
  const [freeText, setFreeText] = usePersistentThingState<string[]>(handle, "freeText");
  const [currentTab, setCurrentTab] = usePersistentThingState<number>(handle, "currentTab");
  const [focusedOption, setFocusedOption] = usePersistentThingState<number>(handle, "focusedOption");
  const [lastRequestId, setLastRequestId] = usePersistentThingState<string>(handle, "lastRequestId");

  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Initialize/reset state when request_id changes
  useEffect(() => {
    if (request.request_id !== lastRequestId) {
      // New request - reset all state
      const initialAnswers: (number | number[])[] = questions.map((q) =>
        q.multiSelect ? [] : -1
      );
      setAnswers(initialAnswers);
      setFreeText(questions.map(() => ""));
      setCurrentTab(0);
      setFocusedOption(0);
      setLastRequestId(request.request_id);
    }
  }, [request.request_id, lastRequestId, questions.length]);

  // Ensure we have valid state
  const safeAnswers = answers ?? questions.map((q) => (q.multiSelect ? [] : -1));
  const safeFreeText = freeText ?? questions.map(() => "");
  const safeCurrentTab = currentTab ?? 0;
  const safeFocusedOption = focusedOption ?? 0;

  const isSubmitTab = safeCurrentTab === questions.length;
  const currentQuestion = isSubmitTab ? null : questions[safeCurrentTab];
  // Options count includes "Other" as the last option
  const optionsCount = currentQuestion ? currentQuestion.options.length + 1 : 0;

  // Check if "Other" button is focused (not the textarea)
  const isOtherButtonFocused = currentQuestion && safeFocusedOption === currentQuestion.options.length;

  // Keyboard navigation - use capture phase to check cursor position before textarea moves it
  useEffect(() => {
    if (!triggerable) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      const textarea = textareaRef.current;
      // Get active element from shadow root (components render in shadow DOM)
      const root = textarea?.getRootNode() as ShadowRoot | Document;
      const isInTextarea = root?.activeElement === textarea;

      // When in textarea, only handle edge cases - let textarea handle everything else
      if (isInTextarea && textarea) {
        if (e.key === "Escape") {
          e.preventDefault();
          trigger({ action: "abort" });
          return;
        }
        if (e.key === "ArrowLeft") {
          // Only handle if cursor is at the very start
          if (textarea.selectionStart === 0 && textarea.selectionEnd === 0) {
            e.preventDefault();
            e.stopPropagation();
            textarea.blur();
            // Stay on Other button - don't change focusedOption
          }
          // Otherwise let textarea handle it (cursor moves left)
          return;
        }
        if (e.key === "ArrowRight") {
          // Only handle if cursor is at the very end
          if (textarea.selectionStart === textarea.value.length &&
              textarea.selectionEnd === textarea.value.length) {
            e.preventDefault();
            e.stopPropagation();
            textarea.blur();
            setCurrentTab(Math.min(numTabs - 1, safeCurrentTab + 1));
            setFocusedOption(0);
          }
          // Otherwise let textarea handle it (cursor moves right)
          return;
        }
        // Let textarea handle all other keys (typing, ArrowUp/Down for multiline, etc.)
        return;
      }

      // Normal navigation when not in textarea
      switch (e.key) {
        case "ArrowLeft":
          e.preventDefault();
          setCurrentTab(Math.max(0, safeCurrentTab - 1));
          setFocusedOption(0);
          break;
        case "ArrowRight":
          // If on Other button, focus the textarea instead of changing tabs
          if (isOtherButtonFocused && textarea) {
            e.preventDefault();
            textarea.focus();
            textarea.setSelectionRange(0, 0);
          } else {
            e.preventDefault();
            setCurrentTab(Math.min(numTabs - 1, safeCurrentTab + 1));
            setFocusedOption(0);
          }
          break;
        case "ArrowUp":
          e.preventDefault();
          if (isSubmitTab) {
            // On submit tab: navigate between Submit (0) and Abort (1)
            setFocusedOption(Math.max(0, safeFocusedOption - 1));
          } else {
            setFocusedOption(Math.max(0, safeFocusedOption - 1));
          }
          break;
        case "ArrowDown":
          e.preventDefault();
          if (isSubmitTab) {
            // On submit tab: navigate between Submit (0) and Abort (1)
            setFocusedOption(Math.min(1, safeFocusedOption + 1));
          } else {
            setFocusedOption(Math.min(optionsCount - 1, safeFocusedOption + 1));
          }
          break;
        case "Enter":
          e.preventDefault();
          if (isSubmitTab) {
            // Execute the focused action
            if (safeFocusedOption === 0) {
              handleSubmit();
            } else {
              handleAbort();
            }
          } else if (currentQuestion) {
            selectOption(safeCurrentTab, safeFocusedOption);
          }
          break;
        case "Escape":
          e.preventDefault();
          trigger({ action: "abort" });
          break;
        case " ": // Space to select
          if (!isSubmitTab && currentQuestion) {
            e.preventDefault();
            selectOption(safeCurrentTab, safeFocusedOption);
          }
          break;
      }
    };

    // Use capture phase to intercept before textarea handles the event
    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [
    triggerable,
    safeCurrentTab,
    safeFocusedOption,
    numTabs,
    optionsCount,
    isSubmitTab,
    isOtherButtonFocused,
    request.request_id,
  ]);

  const selectOption = (qIdx: number, optIdx: number) => {
    const q = questions[qIdx];
    const isOther = optIdx === q.options.length;
    const newAnswers = [...safeAnswers];

    if (q.multiSelect) {
      const current = (newAnswers[qIdx] as number[]) || [];
      if (isOther) {
        // "Other" is mutually exclusive - clear other selections
        newAnswers[qIdx] = [optIdx];
      } else {
        // Toggle this option, but remove "Other" if it was selected
        const withoutOther = current.filter((i) => i !== q.options.length);
        if (withoutOther.includes(optIdx)) {
          newAnswers[qIdx] = withoutOther.filter((i) => i !== optIdx);
        } else {
          newAnswers[qIdx] = [...withoutOther, optIdx];
        }
      }
    } else {
      // Single select
      newAnswers[qIdx] = optIdx;
    }

    setAnswers(newAnswers);
    // Note: Don't auto-focus textarea on selection - user can press ArrowRight to enter it
  };

  const handleFreeTextChange = (qIdx: number, text: string) => {
    const newFreeText = [...safeFreeText];
    newFreeText[qIdx] = text;
    setFreeText(newFreeText);

    // Auto-select "Other" when typing
    const q = questions[qIdx];
    const otherIdx = q.options.length;
    const newAnswers = [...safeAnswers];
    if (q.multiSelect) {
      newAnswers[qIdx] = [otherIdx];
    } else {
      newAnswers[qIdx] = otherIdx;
    }
    setAnswers(newAnswers);
  };

  const isOptionSelected = (qIdx: number, optIdx: number): boolean => {
    const answer = safeAnswers[qIdx];
    if (Array.isArray(answer)) {
      return answer.includes(optIdx);
    }
    return answer === optIdx;
  };

  const getAnswerDisplay = (qIdx: number): string => {
    const q = questions[qIdx];
    const answer = safeAnswers[qIdx];

    if (answer === -1 || (Array.isArray(answer) && answer.length === 0)) {
      return "";
    }

    if (q.multiSelect) {
      const indices = answer as number[];
      if (indices.includes(q.options.length)) {
        return safeFreeText[qIdx] || "(empty free text)";
      }
      return indices.map((i) => q.options[i]?.label || "?").join(", ");
    } else {
      const idx = answer as number;
      if (idx === q.options.length) {
        return safeFreeText[qIdx] || "(empty free text)";
      }
      return q.options[idx]?.label || "?";
    }
  };

  const isUnanswered = (qIdx: number): boolean => {
    const answer = safeAnswers[qIdx];
    if (answer === -1) return true;
    if (Array.isArray(answer) && answer.length === 0) return true;
    return false;
  };

  const buildAnswersPayload = (): Record<string, string> => {
    const result: Record<string, string> = {};
    questions.forEach((q, qIdx) => {
      const answer = safeAnswers[qIdx];
      if (answer === -1 || (Array.isArray(answer) && answer.length === 0)) {
        result[q.question] = "";
      } else if (q.multiSelect) {
        const indices = answer as number[];
        if (indices.includes(q.options.length)) {
          result[q.question] = safeFreeText[qIdx] || "";
        } else {
          result[q.question] = indices.map((i) => q.options[i].label).join(", ");
        }
      } else {
        const idx = answer as number;
        if (idx === q.options.length) {
          result[q.question] = safeFreeText[qIdx] || "";
        } else {
          result[q.question] = q.options[idx].label;
        }
      }
    });
    return result;
  };

  const handleSubmit = async () => {
    if (!triggerable) return;

    // Clear persistent state
    setAnswers(undefined);
    setFreeText(undefined);
    setCurrentTab(undefined);
    setFocusedOption(undefined);
    // Keep lastRequestId so we don't re-init if same request comes back

    await trigger({
      action: "allow",
      answers: buildAnswersPayload(),
    });
  };

  const handleAbort = async () => {
    if (!triggerable) return;

    // Clear persistent state
    setAnswers(undefined);
    setFreeText(undefined);
    setCurrentTab(undefined);
    setFocusedOption(undefined);

    await trigger({ action: "abort" });
  };

  return (
    <div style={styles.container}>
      <div style={styles.header}>Questions from Claude</div>

      {/* Tab bar */}
      <div style={styles.tabBar}>
        {questions.map((q, i) => (
          <button
            type="button"
            key={i}
            style={{
              ...styles.tab,
              ...(safeCurrentTab === i ? styles.tabActive : {}),
            }}
            onClick={() => {
              setCurrentTab(i);
              setFocusedOption(0);
            }}
          >
            {q.header}
          </button>
        ))}
        <button
          type="button"
          style={{
            ...styles.tab,
            ...(isSubmitTab ? styles.tabActive : {}),
          }}
          onClick={() => {
            setCurrentTab(questions.length);
            setFocusedOption(0);
          }}
        >
          Submit
        </button>
      </div>

      {/* Current tab content */}
      {!isSubmitTab && currentQuestion && (
        <div>
          <div style={styles.questionText}>{currentQuestion.question}</div>
          {currentQuestion.multiSelect && (
            <div style={{ ...styles.hint, marginBottom: "8px" }}>
              Select multiple options
            </div>
          )}

          <div style={styles.optionsContainer}>
            {currentQuestion.options.map((opt, i) => {
              const isFocused = safeFocusedOption === i;
              const isSelected = isOptionSelected(safeCurrentTab, i);
              return (
                <button
                  type="button"
                  key={i}
                  style={{
                    ...styles.optionButton,
                    ...(isFocused && isSelected
                      ? styles.buttonFocusedSelected
                      : isFocused
                      ? styles.buttonFocused
                      : isSelected
                      ? styles.buttonSelected
                      : {}),
                  }}
                  onClick={() => selectOption(safeCurrentTab, i)}
                  onMouseEnter={() => setFocusedOption(i)}
                >
                  {opt.label}
                  <span style={styles.optionDescription}>{opt.description}</span>
                </button>
              );
            })}

            {/* "Other" option - horizontal layout with always-visible textarea */}
            {(() => {
              const otherIdx = currentQuestion.options.length;
              const isFocused = safeFocusedOption === otherIdx;
              const isSelected = isOptionSelected(safeCurrentTab, otherIdx);
              return (
                <div style={{ display: "flex", gap: "8px", alignItems: "flex-start" }}>
                  <button
                    type="button"
                    style={{
                      ...styles.button,
                      ...(isFocused && isSelected
                        ? styles.buttonFocusedSelected
                        : isFocused
                        ? styles.buttonFocused
                        : isSelected
                        ? styles.buttonSelected
                        : {}),
                      minWidth: "60px",
                      flexShrink: 0,
                    }}
                    onClick={() => selectOption(safeCurrentTab, otherIdx)}
                    onMouseEnter={() => setFocusedOption(otherIdx)}
                  >
                    Other
                  </button>
                  <textarea
                    ref={textareaRef}
                    value={safeFreeText[safeCurrentTab] || ""}
                    onChange={(e) => handleFreeTextChange(safeCurrentTab, e.target.value)}
                    placeholder="Type your answer... (→ to enter)"
                    style={{
                      ...styles.freeTextArea,
                      flex: 1,
                      minHeight: "40px",
                    }}
                  />
                </div>
              );
            })()}
          </div>
        </div>
      )}

      {/* Submit tab */}
      {isSubmitTab && (
        <div>
          <div style={{ ...styles.questionText, marginBottom: "12px" }}>
            Review your answers:
          </div>

          {questions.map((q, i) => (
            <div
              key={i}
              style={{
                ...styles.summaryItem,
                ...(isUnanswered(i) ? { borderColor: "#d9a54a" } : {}),
              }}
            >
              <div style={styles.summaryQuestion}>{q.question}</div>
              <div style={styles.summaryAnswer}>
                {isUnanswered(i) ? (
                  <span style={styles.warningText}>Not answered</span>
                ) : (
                  getAnswerDisplay(i)
                )}
              </div>
            </div>
          ))}

          <div style={styles.optionsContainer}>
            <button
              type="button"
              style={{
                ...styles.submitButton,
                ...(safeFocusedOption === 0 ? styles.buttonFocused : {}),
                width: "100%",
              }}
              onClick={handleSubmit}
              onMouseEnter={() => setFocusedOption(0)}
            >
              Submit Answers
            </button>
            <button
              type="button"
              style={{
                ...styles.button,
                ...(safeFocusedOption === 1 ? styles.buttonAbortFocused : styles.buttonAbort),
                width: "100%",
              }}
              onClick={handleAbort}
              onMouseEnter={() => setFocusedOption(1)}
            >
              Abort
            </button>
          </div>
        </div>
      )}

      <div style={{ ...styles.hint, marginTop: "8px" }}>
        {isSubmitTab
          ? "\u2190\u2192 tabs, \u2191\u2193 navigate, Enter select, Esc abort"
          : "\u2190\u2192 tabs, \u2191\u2193 options, Enter/Space select, \u2192 into Other, Esc abort"}
      </div>
    </div>
  );
}

// =============================================================================
// Main renderer
// =============================================================================

export function FullRenderer({ handle }: FullRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);
  const [triggerable] = useTriggerable(handle);
  const trigger = useTrigger(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const pendingRequest = lastChunk?.pending_request;

  // Don't render anything if no pending request
  if (!pendingRequest) {
    return null;
  }

  // Render based on request type
  if (pendingRequest.type === "permission") {
    return (
      <PermissionModeRenderer
        request={pendingRequest}
        triggerable={triggerable}
        trigger={trigger}
      />
    );
  } else if (pendingRequest.type === "question") {
    return (
      <QuestionModeRenderer
        request={pendingRequest}
        triggerable={triggerable}
        trigger={trigger}
        handle={handle}
      />
    );
  }

  return null;
}

// =============================================================================
// Ref renderers (dialog is not typically shown as a ref)
// =============================================================================

export function RefRenderer(_props: RefRendererProps) {
  return (
    <span
      style={{
        color: "#888",
        fontFamily: "monospace",
        fontSize: "11px",
      }}
    >
      Dialog
    </span>
  );
}

export function InlineRefRenderer(props: RefRendererProps) {
  return RefRenderer(props);
}

export function PreviewRenderer(_props: PreviewRendererProps) {
  return (
    <span
      style={{
        color: "#888",
        fontFamily: "monospace",
        fontSize: "11px",
      }}
    >
      Dialog
    </span>
  );
}

// Renderer set for use with ThingWatcherRenderConfig
export const renderers = {
  full: FullRenderer,
  ref: RefRenderer,
  inline: InlineRefRenderer,
  preview: PreviewRenderer,
} as RendererSet;
