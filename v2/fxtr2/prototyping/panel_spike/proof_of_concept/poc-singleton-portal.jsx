/*
Notes:

---

External Store for State
I didn't use useState or useReducer in the provider. Why? Because the registry needs to be mutated during effects (register/unregister), and we need fine-grained subscriptions. An external store pattern gives us:

Synchronous reads — no stale closure issues
Controlled re-renders — only subscribers get notified
Effect-safe mutations — we can call register() in useEffect without triggering render-during-render warnings

The useSyncExternalStore hook is React's blessed way to subscribe to external state. It handles edge cases like concurrent rendering and hydration.

---

The Registry Data Structure

// Map from singleton ID → entry
entries: Map<string, {
  activeInstanceId: string | null,
  instances: Map<string, { ref: RefObject }>
}>


For each singleton ID (like `"editor"`), we track:

- **Which instance is active** — a single `instanceId` or `null`
- **All registered instances** — a Map so we can add/remove in O(1) and iterate to pick a successor

Why a `Map` for instances rather than an array? Easier removal (no index shifting), and `Map` preserves insertion order, so "next in line" is deterministic.

## The Lifecycle Flow

Here's what happens when a `<Singleton id="editor">` mounts:

1. useId() generates a stable instanceId (e.g., ":r1:")

2. useEffect runs:
   - Calls registry.register("editor", ":r1:", ref)
   - Registry checks: is there an entry for "editor"?
     - No → create one, this instance becomes active
     - Yes → add to instances Map, stay inactive
   - Registry calls notify() → all subscribers re-render

3. useSyncExternalStore reads the entry
   - Compares entry.activeInstanceId to our instanceId
   - Returns isActive: true or false

4. Component renders:
   - If active → render children
   - If inactive → render fallback

And on unmount:

1. useEffect cleanup runs:
   - Calls registry.unregister("editor", ":r1:")
   - Registry removes instance from Map
   - If this was the active one:
     - Pick next instance from Map (first key)
     - Set as new activeInstanceId
   - notify() → remaining instances re-render
   - The new active one now shows children instead of fallback
Why useId() for Instance Identity?
React's useId() hook gives us a stable identifier that:

Survives re-renders
Is unique per component instance
Works correctly with server-side rendering

We could use useRef(Symbol()) or useRef(crypto.randomUUID()), but useId() is purpose-built for this and integrates with React's internal reconciliation.

---

Why Not Just Use Context State?
You might wonder why not just do:
const [entries, setEntries] = useState(new Map());
A few reasons:

Effect timing — register is called in useEffect. If we called setEntries there, we'd trigger a re-render during commit phase, which can cause issues.
Batching concerns — multiple components mounting in the same render would each call setEntries, potentially causing multiple re-renders or stale state issues.
Subscription granularity — with useSyncExternalStore, React knows exactly when to re-render subscribers. With context state, every consumer re-renders on any change.
*/
import React, {
  createContext,
  useContext,
  useCallback,
  useId,
  useSyncExternalStore,
  useRef,
  useEffect,
  useState,
  useMemo,
} from 'react';

// --- Load ReactDOM from CDN ---

function loadScript(src) {
  return new Promise((resolve, reject) => {
    // Check if already loaded
    if (document.querySelector(`script[src="${src}"]`)) {
      resolve();
      return;
    }
    const script = document.createElement('script');
    script.src = src;
    script.onload = resolve;
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

function ReactDOMLoader({ children }) {
  const [reactDOM, setReactDOM] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    async function loadReactDOM() {
      try {
        // Load React first (needed by ReactDOM)
        await loadScript('https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js');
        // Then ReactDOM
        await loadScript('https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js');

        if (window.ReactDOM?.createPortal) {
          setReactDOM(window.ReactDOM);
        } else {
          setError('ReactDOM loaded but createPortal not available');
        }
      } catch (err) {
        setError(err.message || 'Failed to load ReactDOM');
      }
    }
    loadReactDOM();
  }, []);

  if (error) {
    return (
      <div className="p-4 bg-red-100 text-red-700 rounded m-4">
        Failed to load ReactDOM: {error}
      </div>
    );
  }

  if (!reactDOM) {
    return (
      <div className="p-4 bg-gray-100 text-gray-600 rounded m-4 flex items-center gap-2">
        <div className="animate-spin h-4 w-4 border-2 border-purple-500 border-t-transparent rounded-full" />
        Loading ReactDOM...
      </div>
    );
  }

  return (
    <ReactDOMContext.Provider value={reactDOM}>
      {children}
    </ReactDOMContext.Provider>
  );
}

const ReactDOMContext = createContext(null);

function useReactDOM() {
  const reactDOM = useContext(ReactDOMContext);
  if (!reactDOM) {
    throw new Error('useReactDOM must be used within ReactDOMLoader');
  }
  return reactDOM;
}

// --- Custom implementation of react-reverse-portal ---

function createHtmlPortalNode() {
  const node = document.createElement('div');
  node.dataset.reversePortal = '';
  return node;
}

function InPortal({ node, children }) {
  const reactDOM = useReactDOM();
  if (!node) return null;
  return reactDOM.createPortal(children, node);
}

function OutPortal({ node }) {
  const containerRef = useRef(null);
  const [, forceUpdate] = useState(0);

  useEffect(() => {
    forceUpdate(n => n + 1);
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !node) return;

    container.appendChild(node);

    return () => {
      if (node.parentNode === container) {
        container.removeChild(node);
      }
    };
  }, [node]);

  return <div ref={containerRef} />;
}

// --- Registry (external store) ---

function createRegistry() {
  let entries = new Map();
  const listeners = new Set();

  const notify = () => listeners.forEach((l) => l());

  const getEntry = (id) => entries.get(id);

  // Get or create portal node for an ID (needs portalLib passed in)
  const getOrCreatePortalNode = (id, createHtmlPortalNode) => {
    const entry = entries.get(id);
    if (entry?.portalNode) return entry.portalNode;

    const portalNode = createHtmlPortalNode();

    const newEntry = {
      ...entry,
      portalNode,
      activeInstanceId: entry?.activeInstanceId ?? null,
      instances: entry?.instances ?? new Map(),
    };

    entries = new Map(entries);
    entries.set(id, newEntry);
    // Don't notify here - this is just lazy initialization
    return portalNode;
  };

  const register = (id, instanceId, ref) => {
    const oldEntry = entries.get(id);
    const newInstances = new Map(oldEntry?.instances);
    newInstances.set(instanceId, { ref });

    const newEntry = {
      portalNode: oldEntry?.portalNode ?? null,
      activeInstanceId: oldEntry?.activeInstanceId ?? instanceId,
      instances: newInstances,
    };

    entries = new Map(entries);
    entries.set(id, newEntry);
    notify();
  };

  const unregister = (id, instanceId) => {
    const oldEntry = entries.get(id);
    if (!oldEntry) return;

    const newInstances = new Map(oldEntry.instances);
    newInstances.delete(instanceId);

    let newActiveId = oldEntry.activeInstanceId;
    if (newActiveId === instanceId) {
      newActiveId = newInstances.keys().next().value ?? null;
    }

    const newEntry = {
      portalNode: oldEntry.portalNode,
      activeInstanceId: newActiveId,
      instances: newInstances,
    };

    entries = new Map(entries);
    entries.set(id, newEntry);
    notify();
  };

  const takeOwnership = (id, instanceId) => {
    const oldEntry = entries.get(id);
    if (!oldEntry || !oldEntry.instances.has(instanceId)) return;

    const newEntry = {
      ...oldEntry,
      activeInstanceId: instanceId,
    };

    entries = new Map(entries);
    entries.set(id, newEntry);
    notify();
  };

  const getActiveRef = (id) => {
    const entry = entries.get(id);
    if (!entry || !entry.activeInstanceId) return null;
    return entry.instances.get(entry.activeInstanceId)?.ref ?? null;
  };

  const subscribe = (listener) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  };

  const getSnapshot = () => entries;

  return {
    getEntry,
    getOrCreatePortalNode,
    register,
    unregister,
    takeOwnership,
    getActiveRef,
    subscribe,
    getSnapshot,
  };
}

// --- Context ---

const SingletonContext = createContext(null);

function SingletonProvider({ children }) {
  const registryRef = useRef();
  if (!registryRef.current) {
    registryRef.current = createRegistry();
  }
  return (
    <SingletonContext.Provider value={registryRef.current}>
      {children}
    </SingletonContext.Provider>
  );
}

// --- SingletonContent: defines WHAT to render (via InPortal) ---

function SingletonContent({ id, children }) {
  const registry = useContext(SingletonContext);

  if (!registry) {
    throw new Error('SingletonContent must be used within SingletonProvider');
  }

  // Get or create the portal node for this ID
  const portalNode = useMemo(
    () => registry.getOrCreatePortalNode(id, createHtmlPortalNode),
    [registry, id]
  );

  return (
    <InPortal node={portalNode}>
      {children}
    </InPortal>
  );
}

// --- Hook for slots ---

function useSingletonSlot(id) {
  const registry = useContext(SingletonContext);
  if (!registry) {
    throw new Error('useSingletonSlot must be used within SingletonProvider');
  }

  const instanceId = useId();
  const containerRef = useRef(null);

  const entry = useSyncExternalStore(
    registry.subscribe,
    () => registry.getEntry(id),
    () => registry.getEntry(id)
  );

  useEffect(() => {
    registry.register(id, instanceId, containerRef);
    return () => registry.unregister(id, instanceId);
  }, [id, instanceId, registry]);

  const isActive = entry?.activeInstanceId === instanceId;
  const portalNode = entry?.portalNode ?? null;

  const takeOwnership = useCallback(() => {
    registry.takeOwnership(id, instanceId);
  }, [registry, id, instanceId]);

  const jumpToActive = useCallback(() => {
    const activeRef = registry.getActiveRef(id);
    activeRef?.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [registry, id]);

  return { isActive, portalNode, takeOwnership, jumpToActive, containerRef };
}

// --- SingletonSlot: defines WHERE to render (via OutPortal) ---

function SingletonSlot({ id, fallback }) {
  const { isActive, portalNode, takeOwnership, jumpToActive, containerRef } = useSingletonSlot(id);

  if (isActive && portalNode) {
    return (
      <div ref={containerRef}>
        <OutPortal node={portalNode} />
      </div>
    );
  }

  if (fallback) {
    return <>{fallback({ takeOwnership, jumpToActive })}</>;
  }

  return (
    <div className="p-3 bg-gray-100 rounded border-2 border-dashed border-gray-300">
      <p className="text-sm text-gray-600 mb-2">Another instance is active</p>
      <div className="flex gap-2">
        <button
          onClick={jumpToActive}
          className="px-2 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600"
        >
          Jump to active
        </button>
        <button
          onClick={takeOwnership}
          className="px-2 py-1 text-xs bg-green-500 text-white rounded hover:bg-green-600"
        >
          Take ownership
        </button>
      </div>
    </div>
  );
}

// --- Demo Components ---

function EditorWidget() {
  const [text, setText] = useState('Type something here...');
  const [count, setCount] = useState(0);

  return (
    <div className="p-4 bg-gradient-to-br from-purple-500 to-indigo-600 rounded-lg text-white shadow-lg">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-xl">📝</span>
          <span className="font-bold">Active Editor</span>
        </div>
        <div className="text-xs bg-white/20 px-2 py-1 rounded">
          Edits: {count}
        </div>
      </div>
      <textarea
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setCount(c => c + 1);
        }}
        className="w-full p-2 rounded bg-white/20 text-white placeholder-white/50 text-sm resize-none"
        rows={3}
      />
      <p className="text-xs mt-2 opacity-75">
        ✨ This instance persists! State is preserved when ownership moves.
      </p>
    </div>
  );
}

function CollapsibleBox({ title, defaultOpen = true, children, color = 'slate' }) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  const colorClasses = {
    slate: 'bg-slate-50 border-slate-200',
    blue: 'bg-blue-50 border-blue-200',
    green: 'bg-green-50 border-green-200',
    amber: 'bg-amber-50 border-amber-200',
    rose: 'bg-rose-50 border-rose-200',
  };

  return (
    <div className={`rounded-lg border-2 ${colorClasses[color]} overflow-hidden`}>
      <div className="flex items-center justify-between p-3 bg-white/50">
        <span className="font-medium text-gray-700">{title}</span>
        <label className="flex items-center gap-2 cursor-pointer">
          <span className="text-sm text-gray-500">{isOpen ? 'Visible' : 'Hidden'}</span>
          <input
            type="checkbox"
            checked={isOpen}
            onChange={(e) => setIsOpen(e.target.checked)}
            className="w-4 h-4 rounded"
          />
        </label>
      </div>
      {isOpen && <div className="p-3 space-y-3">{children}</div>}
    </div>
  );
}

function SlotLabel({ number }) {
  return (
    <div className="text-xs text-gray-400 mb-1">Slot #{number}</div>
  );
}

// --- Main App ---

function AppContent() {
  return (
    <SingletonProvider>
      {/* Define the singleton content ONCE at the top level */}
      <SingletonContent id="editor">
        <EditorWidget />
      </SingletonContent>

      <div className="min-h-screen bg-gray-100 p-6">
        <div className="max-w-4xl mx-auto">
          <h1 className="text-2xl font-bold text-gray-800 mb-2">
            Singleton with Reverse Portal
          </h1>
          <p className="text-gray-600 mb-2">
            This version uses a reverse portal pattern to
            preserve component state when ownership moves between slots.
          </p>
          <p className="text-gray-600 mb-6">
            <strong>Try it:</strong> Type something in the editor, then click "Take ownership"
            on a different slot. Your text and edit count are preserved!
          </p>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Box with single slot */}
            <CollapsibleBox title="Box A (1 slot)" color="blue">
              <SlotLabel number={1} />
              <SingletonSlot id="editor" />
            </CollapsibleBox>

            {/* Box with two slots */}
            <CollapsibleBox title="Box B (2 slots)" color="green">
              <SlotLabel number={2} />
              <SingletonSlot id="editor" />

              <SlotLabel number={3} />
              <SingletonSlot id="editor" />
            </CollapsibleBox>

            {/* Box with single slot, starts closed */}
            <CollapsibleBox title="Box C (1 slot, starts hidden)" defaultOpen={false} color="amber">
              <SlotLabel number={4} />
              <SingletonSlot id="editor" />
            </CollapsibleBox>

            {/* Box with three slots */}
            <CollapsibleBox title="Box D (3 slots)" color="rose">
              <SlotLabel number={5} />
              <SingletonSlot id="editor" />

              <SlotLabel number={6} />
              <SingletonSlot id="editor" />

              <SlotLabel number={7} />
              <SingletonSlot id="editor" />
            </CollapsibleBox>
          </div>
        </div>
      </div>
    </SingletonProvider>
  );
}

export default function App() {
  return (
    <ReactDOMLoader>
      <AppContent />
    </ReactDOMLoader>
  );
}
