import React, { useState, useEffect } from 'react';
import { BasicCollapsibleViewRoot } from './BasicCollapsibleView';
import { EntityView, renderNestedEntitiesByCid, renderPythonPath, createEntityDefinitionPathAwareLinkRenderer, ExternalCidReferenceView } from './HubValueView';
import { convertEscapedJson, CidString } from './EscapedJsonTypes';
import { LinkRendererProvider } from './LinkRendererContext';
import { createChildPathMapping } from './module_util';

interface DebugVisualizerProps {
  moduleCid?: string;
}

interface TaskRun {
  run_name: string;
  state: string;
  status_message: string | null;
  run_cid: string | null;
}

interface TaskRunData {
  task_cid: string;
  runs: TaskRun[];
  canonical_path: string | null;
  entity_defn_data: any;
  child_path_mapping: Map<string, { moduleCid: string; offset: number }> | null;
}

interface DependencyInfo {
  cid: string;
  name: string | null;
  loading: boolean;
  error: string | null;
}

function DependencyDisplay({ depCid }: { depCid: any }) {
  const [depInfo, setDepInfo] = useState<DependencyInfo>({
    cid: depCid?.base32_cid_str || depCid,
    name: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    const loadDependencyName = async () => {
      try {
        const response = await fetch(`/block/${depInfo.cid}`);
        if (!response.ok) {
          throw new Error(`Failed to fetch: ${response.statusText}`);
        }
        const rawData = await response.json();
        const convertedData = convertEscapedJson(rawData);
        const name = convertedData?.name || convertedData?.namespace_id || null;
        setDepInfo(prev => ({ ...prev, name, loading: false }));
      } catch (err) {
        setDepInfo(prev => ({
          ...prev,
          loading: false,
          error: err instanceof Error ? err.message : 'Unknown error',
        }));
      }
    };

    loadDependencyName();
  }, [depInfo.cid]);

  const displayText = depInfo.loading
    ? 'Loading...'
    : depInfo.error
    ? depInfo.cid
    : depInfo.name
    ? `${depInfo.name} (${depInfo.cid})`
    : depInfo.cid;

  return (
    <li style={{ marginBottom: '5px' }}>
      <a
        href={`/view/collection/${depInfo.cid}`}
        style={{
          color: '#0066cc',
          textDecoration: 'none',
          padding: '5px 10px',
          border: '1px solid #ddd',
          borderRadius: '3px',
          display: 'inline-block',
        }}
      >
        {displayText}
      </a>
    </li>
  );
}

interface EntityDefinitionDisplayProps {
  moduleCid: string;
  entityIndex: number;
  moduleData: any;
}

function EntityDefinitionDisplay({ moduleCid, entityIndex, moduleData }: EntityDefinitionDisplayProps) {
  const [entityDefnData, setEntityDefnData] = useState<any>(null);
  const [entityData, setEntityData] = useState<any>(null);
  const [childPathMapping, setChildPathMapping] = useState<Map<string, { moduleCid: string; offset: number }> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadEntityDefinition = async () => {
      try {
        // Get the entity definition at the specified index from the already loaded module data
        const entityDefnLinks = moduleData?.entity_defns || [];
        if (entityIndex >= entityDefnLinks.length) {
          throw new Error(`Entity definition index ${entityIndex} out of range`);
        }

        const entityDefnLink = entityDefnLinks[entityIndex];
        const entityDefnCid = (entityDefnLink as CidString).base32_cid_str;

        if (!entityDefnCid) {
          throw new Error('No entity definition CID found');
        }

        // Fetch the entity definition data
        const entityDefnResponse = await fetch(`/block/${entityDefnCid}`);
        if (!entityDefnResponse.ok) {
          throw new Error(`Failed to fetch entity definition: ${entityDefnResponse.statusText}`);
        }
        const rawDefnData = await entityDefnResponse.json();
        const defnData = convertEscapedJson(rawDefnData);
        setEntityDefnData(defnData);

        // Create child path mapping for Stage 8b link renderer
        const pathMapping = createChildPathMapping(moduleCid, defnData);
        setChildPathMapping(pathMapping);

        // Fetch the actual entity data
        const entityLink = defnData?.entity;
        const entityCid = (entityLink as CidString).base32_cid_str;
        if (entityCid) {
          const entityResponse = await fetch(`/block/${entityCid}`);
          if (entityResponse.ok) {
            const rawEntData = await entityResponse.json();
            const entData = convertEscapedJson(rawEntData);
            setEntityData(entData);
          }
        }

        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
        setLoading(false);
      }
    };

    loadEntityDefinition();
  }, [moduleCid, entityIndex, moduleData]);

  if (loading) {
    return <div style={{ color: '#666', fontSize: '14px' }}>Loading entity definition #{entityIndex}...</div>;
  }

  if (error || !entityDefnData) {
    return <div style={{ color: '#cc0000', fontSize: '14px' }}>Error loading entity definition #{entityIndex}: {error}</div>;
  }

  // Generate the assignment target
  const canonicalPath = entityDefnData.canonical_access_path;
  const assignmentTarget = canonicalPath
    ? renderPythonPath('(root)', canonicalPath)
    : null;

  // Create link renderer for this entity definition
  const linkRenderer = childPathMapping
    ? createEntityDefinitionPathAwareLinkRenderer(
        childPathMapping,
        moduleCid,
        entityDefnData?.canonical_access_path
      )
    : renderNestedEntitiesByCid;

  return (
    <div style={{
      marginBottom: '20px',
      fontSize: '14px',
      fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
      whiteSpace: 'pre'
    }}>
      {/* Comment hyperlink */}
      <a
        href={`/view/collection/${moduleCid}/entdefn/${entityIndex}`}
        style={{ color: '#888', fontSize: '13px' }}
      >
        # Entity Definition {entityIndex}{"\n"}
      </a>

      {/* Assignment line */}
      {assignmentTarget && (
        <span style={{ color: '#333', marginRight: '8px' }}>
          {assignmentTarget} =
        </span>
      )}
      {entityData ? (
        <LinkRendererProvider linkRenderer={linkRenderer}>
          <EntityView
            entitySpec={entityData.$typespec}
            entityData={entityData}
            collapseBehavior={null}
            startExpanded={false}
          />
        </LinkRendererProvider>
      ) : (
        <span style={{ color: '#666' }}>Entity data unavailable</span>
      )}
    </div>
  );
}

interface TasksDisplayProps {
  moduleCid: string;
  moduleData: any;
}

function TasksDisplay({ moduleCid, moduleData }: TasksDisplayProps) {
  const [orderedTasks, setOrderedTasks] = useState<TaskRunData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadTasksAndRuns = async () => {
      try {
        // Find all tasks in the module by looking at entity definitions
        const entityDefns = moduleData?.entity_defns || [];
        const taskPromises: Promise<TaskRunData | null>[] = [];

        for (let i = 0; i < entityDefns.length; i++) {
          const entityDefnLink = entityDefns[i];
          const entityDefnCid = (entityDefnLink as CidString).base32_cid_str;

          if (!entityDefnCid) {
            taskPromises.push(Promise.resolve(null));
            continue;
          }

          taskPromises.push(
            (async (): Promise<TaskRunData | null> => {
              try {
                // Fetch the entity definition
                const entityDefnResponse = await fetch(`/block/${entityDefnCid}`);
                if (!entityDefnResponse.ok) return null;

                const rawDefnData = await entityDefnResponse.json();
                const defnData = convertEscapedJson(rawDefnData);

                // Check if the entity is a task
                const entityLink = defnData?.entity;
                const entityCid = (entityLink as CidString).base32_cid_str;
                if (!entityCid) return null;

                // Fetch the actual entity to check if it's a task
                const entityResponse = await fetch(`/block/${entityCid}`);
                if (!entityResponse.ok) return null;

                const rawEntityData = await entityResponse.json();
                const entityData = convertEscapedJson(rawEntityData);

                // Check if this entity is a task (has $typespec.$kind: "task")
                if (entityData?.$typespec?.$kind !== 'task') return null;

                // Create child path mapping for this entity definition (like EntityDefinitionDisplay does)
                const pathMapping = createChildPathMapping(moduleCid, defnData);

                // Fetch runs for this task
                const runsResponse = await fetch(`/task/${entityCid}/runs`);
                let runsData = { runs: [] };
                if (runsResponse.ok) {
                  runsData = await runsResponse.json();
                }

                // Generate the canonical path string like EntityDefinitionDisplay does
                const canonicalPath = defnData.canonical_access_path;
                const canonicalPathStr = canonicalPath
                  ? renderPythonPath('(root)', canonicalPath)
                  : null;

                return {
                  task_cid: entityCid,
                  runs: runsData.runs || [],
                  canonical_path: canonicalPathStr,
                  entity_defn_data: defnData,
                  child_path_mapping: pathMapping,
                };
              } catch (err) {
                console.error(`Error processing entity definition ${i}:`, err);
                return null;
              }
            })()
          );
        }

        const taskResults = await Promise.all(taskPromises);
        // Filter out null results and preserve order
        const validTasks = taskResults.filter((task): task is TaskRunData => task !== null);
        setOrderedTasks(validTasks);
        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
        setLoading(false);
      }
    };

    loadTasksAndRuns();
  }, [moduleCid, moduleData]);

  if (loading) {
    return <div style={{ color: '#666', fontSize: '14px' }}>Loading tasks and runs...</div>;
  }

  if (error) {
    return <div style={{ color: '#cc0000', fontSize: '14px' }}>Error loading tasks: {error}</div>;
  }

  if (orderedTasks.length === 0) {
    return <div style={{ color: '#666', fontSize: '14px' }}>No tasks found in this module.</div>;
  }

  return (
    <div style={{ marginBottom: '20px' }}>
      <h2>Tasks and Runs ({orderedTasks.length})</h2>
      <div style={{
        background: '#f8f9fa',
        padding: '15px',
        borderRadius: '4px',
        border: '1px solid #e9ecef',
        whiteSpace: 'pre',
      }}>
        {orderedTasks.map((taskRunData) => (
          <TaskWithRunsDisplay key={taskRunData.task_cid} taskCid={taskRunData.task_cid} taskRunData={taskRunData} moduleCid={moduleCid} />
        ))}
      </div>
    </div>
  );
}

interface TaskWithRunsDisplayProps {
  taskCid: string;
  taskRunData: TaskRunData;
  moduleCid: string;
}

function TaskWithRunsDisplay({ taskCid, taskRunData, moduleCid }: TaskWithRunsDisplayProps) {
  const [taskData, setTaskData] = useState<any>(null);
  const [runEntities, setRunEntities] = useState<Map<string, any>>(new Map());
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadTaskAndRunData = async () => {
      try {
        // Load task data
        const response = await fetch(`/block/${taskCid}`);
        if (response.ok) {
          const rawData = await response.json();
          const data = convertEscapedJson(rawData);
          setTaskData(data);
        }

        // Load run entities for completed runs that have run_cids
        const runEntityPromises = taskRunData.runs
          .filter(run => run.run_cid && run.state === 'completed')
          .map(async run => {
            try {
              const runResponse = await fetch(`/block/${run.run_cid}`);
              if (runResponse.ok) {
                const rawRunData = await runResponse.json();
                const runData = convertEscapedJson(rawRunData);
                setRunEntities(prev => new Map(prev.set(run.run_cid!, runData)));
              }
            } catch (err) {
              console.error(`Error loading run entity ${run.run_cid}:`, err);
            }
          });

        await Promise.all(runEntityPromises);
      } catch (err) {
        console.error(`Error loading task data for ${taskCid}:`, err);
      } finally {
        setLoading(false);
      }
    };

    loadTaskAndRunData();
  }, [taskCid, taskRunData.runs]);

  if (loading) {
    return <div style={{ color: '#666', fontSize: '14px', marginBottom: '10px' }}>
      Loading task {taskCid.substring(0, 12)}...
    </div>;
  }

  const completedRuns = taskRunData.runs.filter(run => run.state === 'completed');
  const otherRuns = taskRunData.runs.filter(run => run.state !== 'completed');

  return (
    <div style={{
      marginBottom: '20px',
      fontSize: '14px',
      fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
    }}>
      {/* Task header */}
      <div style={{ marginBottom: '10px' }}>
        <a
          href={`/view/entity/${taskCid}`}
          style={{ color: '#888', fontSize: '13px' }}
        >
          # Task {taskRunData.canonical_path || taskCid}
        </a>
      </div>

      {/* Task definition */}
      {taskData && (
        <div style={{ marginBottom: '10px', marginLeft: '10px' }}>
          <span style={{ color: '#333', marginRight: '8px' }}>
            {taskRunData.canonical_path && `${taskRunData.canonical_path} =`}
          </span>
          <LinkRendererProvider linkRenderer={
            taskRunData.child_path_mapping
              ? createEntityDefinitionPathAwareLinkRenderer(
                  taskRunData.child_path_mapping,
                  moduleCid,
                  taskRunData.entity_defn_data?.canonical_access_path
                )
              : renderNestedEntitiesByCid
          }>
            <EntityView
              entitySpec={taskData.$typespec}
              entityData={taskData}
              collapseBehavior={null}
              startExpanded={false}
            />
          </LinkRendererProvider>
        </div>
      )}

      {/* Completed runs */}
      {completedRuns.length > 0 && (
        <div style={{ marginLeft: '10px', marginBottom: '10px' }}>
          <div style={{ fontWeight: 'bold', marginBottom: '5px' }}>
            Completed Runs ({completedRuns.length}):
          </div>
          {completedRuns.map(run => {
            const runEntity = run.run_cid ? runEntities.get(run.run_cid) : null;

            return (
              <div key={run.run_name} style={{ marginLeft: '10px', marginBottom: '8px' }}>
                <div>
                  <strong>{run.run_name}</strong>
                  {run.run_cid && (
                    <>
                      {' → '}
                      <a
                        href={`/view/entity/${run.run_cid}`}
                        style={{ color: '#0066cc', textDecoration: 'none' }}
                      >
                        Run {run.run_cid}
                      </a>
                    </>
                  )}
                </div>

                {/* Show run_data and provenance as EntityView if we have the run entity */}
                {runEntity && (
                  <div style={{ marginLeft: '10px', marginTop: '4px' }}>
                    {runEntity.run_data && (
                      <div style={{ marginBottom: '4px' }}>
                        <span style={{ fontWeight: 'bold' }}>Result: </span>
                        <LinkRendererProvider linkRenderer={renderNestedEntitiesByCid}>
                          <ExternalCidReferenceView cid={new CidString(runEntity.run_data)} />
                        </LinkRendererProvider>
                      </div>
                    )}
                    {runEntity.provenance && (
                      <div style={{ marginBottom: '4px' }}>
                        <span style={{ fontWeight: 'bold' }}>Provenance: </span>
                        <LinkRendererProvider linkRenderer={renderNestedEntitiesByCid}>
                          <ExternalCidReferenceView cid={new CidString(runEntity.provenance)} />
                        </LinkRendererProvider>
                      </div>
                    )}
                  </div>
                )}

                {run.status_message && (
                  <div style={{ color: '#666', fontSize: '13px', marginLeft: '10px' }}>
                    {run.status_message.split('\n')[0]}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Other runs */}
      {otherRuns.length > 0 && (
        <div style={{ marginLeft: '10px' }}>
          <div style={{ fontWeight: 'bold', marginBottom: '5px' }}>
            Other Runs ({otherRuns.length}):
          </div>
          {otherRuns.map(run => (
            <div key={run.run_name} style={{ marginLeft: '10px', marginBottom: '5px' }}>
              <span>
                <strong>{run.run_name}</strong> [{run.state}]
              </span>
              {run.status_message && (
                <div style={{ color: '#666', fontSize: '13px', marginLeft: '10px' }}>
                  {run.status_message.split('\n')[0]}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* No runs */}
      {taskRunData.runs.length === 0 && (
        <div style={{ color: '#666', fontSize: '13px', marginLeft: '10px' }}>
          No runs found for this task.
        </div>
      )}
    </div>
  );
}

export function ModulePage({ moduleCid }: DebugVisualizerProps) {
  const [jsonData, setJsonData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadModuleData = async () => {
      if (!moduleCid) {
        setError('No module CID provided');
        setLoading(false);
        return;
      }

      try {
        const response = await fetch(`/block/${moduleCid}`);
        if (!response.ok) {
          throw new Error(`Failed to fetch: ${response.statusText}`);
        }
        const rawData = await response.json();
        const convertedData = convertEscapedJson(rawData);
        setJsonData(convertedData);
        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
        setLoading(false);
      }
    };

    loadModuleData();
  }, [moduleCid]);

  if (loading) {
    return <div>Loading...</div>;
  }

  if (error) {
    console.error(error);
    return <div>Error: {error}</div>;
  }

  // Get collection name - prefer name if it exists and is not null/empty, otherwise use namespace_id
  let collectionName = 'Unknown';
  if (jsonData) {
    if (jsonData.name && jsonData.name !== '') {
      collectionName = jsonData.name;
    } else if (jsonData.namespace_id) {
      collectionName = jsonData.namespace_id;
    }
  }

  const namespaceId = jsonData?.namespace_id || 'Unknown';
  const dependencies = Array.isArray(jsonData?.dependencies) ? jsonData.dependencies : [];
  const entityDefns = jsonData?.entity_defns || [];

  return (
      <div style={{ padding: '20px', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif' }}>
      <h1>Collection: {collectionName}</h1>
      <p style={{ color: '#666', fontSize: '14px', marginBottom: '20px' }}>
        Namespace: <code style={{ background: '#f8f9fa', padding: '2px 4px', borderRadius: '3px' }}>{namespaceId}</code>
      </p>

      {dependencies.length > 0 && (
        <div style={{ marginBottom: '20px' }}>
          <h2>Dependencies</h2>
          <ul style={{ listStyle: 'none', padding: 0 }}>
            {dependencies.map((depCid: any, index: number) => (
              <DependencyDisplay key={index} depCid={depCid} />
            ))}
          </ul>
        </div>
      )}

      <TasksDisplay moduleCid={moduleCid!} moduleData={jsonData} />

      <div style={{ marginBottom: '20px' }}>
        <h2>Entity Definitions ({entityDefns.length})</h2>
        {entityDefns.length > 0 ? (
          <div style={{
            background: '#f8f9fa',
            padding: '15px',
            borderRadius: '4px',
            border: '1px solid #e9ecef'
          }}>
            {entityDefns.map((_: any, index: number) => (
              <EntityDefinitionDisplay
                key={index}
                moduleCid={moduleCid!}
                entityIndex={index}
                moduleData={jsonData}
              />
            ))}
          </div>
        ) : (
          <p>No entity definitions found.</p>
        )}
      </div>

      <details style={{ marginBottom: '20px' }}>
        <summary style={{ cursor: 'pointer', fontWeight: 'bold' }}>Show/Hide Collection Entity Data</summary>
        <div style={{
          background: '#f8f9fa',
          padding: '15px',
          borderRadius: '4px',
          overflow: 'auto',
          whiteSpace: 'pre',
          border: '1px solid #e9ecef',
          fontSize: '12px'
        }}>
          <LinkRendererProvider linkRenderer={renderNestedEntitiesByCid}>
            <EntityView
              entitySpec={jsonData.$typespec}
              entityData={jsonData}
              collapseBehavior={null}
              startExpanded={true}
            />
          </LinkRendererProvider>
        </div>
      </details>
      <details style={{ marginBottom: '20px' }}>
        <summary style={{ cursor: 'pointer', fontWeight: 'bold' }}>Show/Hide Raw Collection Data</summary>
        <div style={{
          background: '#f8f9fa',
          padding: '15px',
          borderRadius: '4px',
          overflow: 'auto',
          whiteSpace: 'pre',
          border: '1px solid #e9ecef',
          fontSize: '12px'
        }}>
          <BasicCollapsibleViewRoot value={jsonData} />
        </div>
      </details>
      </div>
  );
}
