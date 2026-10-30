# Phase 0 Step 3: Model Picker + Init Message Handling

## Problem

We need:
1. A model picker UI that lets users type a model name and submit it to change the active model
2. Both the model picker and permission mode picker should initialize from the "init" SystemMessage that arrives at session start

## Solution Overview

Create a `ModelController` (parallel to `PermissionModeController`) with its own renderer (`ModelPicker.tsx`). The renderer is a text input where placeholder shows current model. Handle "init" SystemMessage in the event stream to initialize both controllers.

## Files to Change

### New Files
- `clydra/model.py` - ModelController, snapshotter, anchor capturer
- `clydra/renderers/ModelPicker.tsx` - Text input renderer

### Modified Files
- `clydra/main.py` - Add model_worker, detect "init" SystemMessage, register model controller
- `clydra/snapshotter.py` - Add model controller to CHILD_LINKS
- `clydra/renderers/ClydraRoot.tsx` - Embed ModelPicker next to PermissionMode

## Implementation Details

### `clydra/model.py`

```python
@dataclass
class ModelController:
    current_model: str = ""  # Empty string = unknown/default
    error: bool = False      # True if last set_model failed
    _manager: ClydraManager | None
```

Snapshotter returns `{"current_model": str, "error": bool}`.

### `clydra/main.py` changes

1. **`model_worker()`** - Background task similar to `permission_mode_worker`:
   - Receives `{model: "..."}` triggers
   - If empty string, ignore (do nothing)
   - Call `client.set_model(model)`
   - On success: update `current_model`, clear `error`
   - On failure: set `error=True`, push synthetic error event to manager.events

2. **Init message handling** - In `run_session()`, after appending each event:
   ```python
   if event_dict.get("_event_type") == "SystemMessage":
       data = event_dict.get("data", {})
       if data.get("subtype") == "init":
           if "model" in data:
               model_ctrl.current_model = data["model"]
           if "permissionMode" in data:
               perm_mode_ctrl.current_mode = data["permissionMode"]
   ```

3. **ClydraInternalError event** - When set_model fails:
   ```python
   manager.events.append({
       "_event_type": "ClydraInternalError",
       "message": f"Failed to set model: {e}",
       "timestamp": datetime.now().isoformat(),
   })
   ```

### `clydra/renderers/ModelPicker.tsx`

- Text input with `placeholder={currentModel}`
- State: `inputValue` for current typed text
- Styling:
  - Input text color: black when empty/placeholder shown, orange when user has typed unsaved content
  - Border: red when `error=true`, normal otherwise
- On submit (Enter key or blur? Just Enter):
  - If `inputValue` is empty, do nothing
  - Otherwise trigger `{model: inputValue}` and clear input
- On successful model change (currentModel changes), clear error state visually

### `clydra/snapshotter.py`

Add `ThingRef(key=MODEL_KEY)` to `CHILD_LINKS`.

### `clydra/renderers/ClydraRoot.tsx`

Add another `<ChildNodeView>` for the model picker, placed next to permission mode in the footer area.

## Implementation Order

1. [x] Create `clydra/model.py` with ModelController, snapshotter, anchor capturer
2. [x] Create `clydra/renderers/ModelPicker.tsx`
3. [x] Update `clydra/main.py`:
   - Import and register model controller
   - Add model_worker
   - Add init message detection in run_session
4. [x] Update `clydra/snapshotter.py` to add MODEL_KEY to CHILD_LINKS
5. [x] Update `clydra/renderers/ClydraRoot.tsx` to embed ModelPicker
6. [x] Update CODE_OVERVIEW.md
