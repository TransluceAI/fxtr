# Implementation Questions and Uncertainties

## Task and Run Visualization Implementation

### Design Decisions Made

1. **Task Discovery Method**: I implemented task discovery by fetching each entity definition and checking if the underlying entity has `$typespec.$kind === 'task'`. This requires multiple API calls but avoids having to precompute task lists.

2. **Run Data Fetching**: I chose to fetch run entities for completed runs to extract `run_data` and `provenance` CID links. This adds additional API calls but provides richer information.

3. **UI Placement**: I placed the Tasks and Runs section above the Entity Definitions section as requested, which maintains a logical flow from high-level (tasks/runs) to low-level (all entities).

### Potential Issues and Simplifications

1. **Performance**: The current implementation makes many parallel API calls (one per entity definition to check if it's a task, plus additional calls for runs and run entities). For modules with many entity definitions, this could be slow or overwhelm the server.

2. **Error Handling**: I implemented basic error handling for individual API calls, but failures are logged to console rather than shown in UI. A production implementation might want more robust error reporting.

3. **Run Entity Structure**: I assumed that run entities follow the `Run` dataclass structure with `run_data` and `provenance` fields as CID objects with `base32_cid_str` property. If the actual structure differs, the links won't work.

4. **CID Display**: I truncated CIDs to 12 characters for display. This might not be sufficient for uniqueness in all cases.

### Questions for Future Development

1. **Caching**: Should task discovery and run data be cached to avoid repeated API calls?

2. **Pagination**: For modules with many tasks or runs, should we implement pagination or lazy loading?

3. **Real-time Updates**: Should the UI poll for new runs or provide a refresh mechanism?

4. **Task Hierarchy**: Should we display task dependencies or group related tasks together?
