import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useTransition,
} from "react";
import { atom } from "jotai";
import {
  EntityID,
  InvocationPanel,
  invocationRefKey,
  type ArrayData,
  type JsonValue,
  type Spawn,
} from "fxtr-view";
import { RenderInvocation, chosenRenderer, renderersFor } from "fxtr-view-shell-util";
import {
  Panel,
  PanelGroup,
  PanelResizeHandle,
  type ImperativePanelHandle,
} from "react-resizable-panels";
import {
  pokeLive,
  retryAndReset,
  useOverviewSnapshot,
  useLevel,
  useLivePolling,
} from "./api/live";
import { useFreshIds } from "./lib/fresh";
import {
  LevelStateContext,
  type CardTableState,
  type LevelState,
} from "./lib/levelState";
import { useScrollAnchor } from "./lib/scrollAnchor";
import { useTabIcon } from "./lib/tabIcon";
import { useFollowLaunches } from "./lib/launches";
import { OpenEntityContext } from "./lib/openEntity";
import { CardSpotlightContext, createSpotlightStore, type CardSpotlight } from "./lib/cardSpotlight";
import { Store, useStableCallback } from "./lib/store";
import { indexSteps, levelTrail, mappedKeyOf } from "./lib/steps";
import { OpenLevelContext, type LevelOpener } from "./lib/openLevel";
import type { JobSummary, KeyValue, LevelRef } from "./types";
import { DetailPane } from "./components/DetailPane";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Flowchart } from "./components/Flowchart";
import { SliceDims } from "./components/SliceDims";
import { RunningIndicator } from "./components/RunningIndicator";
import { JobPicker } from "./components/JobPicker";
import { Toasts } from "./components/Toasts";
import { KeyboardShortcuts } from "./components/KeyboardShortcuts";
import { LevelHeader } from "./components/LevelHeader";
import { LiveIndicator } from "./components/LiveIndicator";
import { LevelViews } from "./components/LevelViews";
import { ExperimentReadmeLink } from "./components/ExperimentReadme";
import { ThemeToggle } from "./components/ThemeToggle";
import {
  FlowchartToggle,
  Minimap,
  WorkflowBreadcrumbs,
  type CardGeometry,
  createViewportStore,
} from "./components/Minimap";
import { ReturnCard } from "./components/ReturnCard";
import { Loading, Problem } from "./components/Status";
import { StepCard } from "./components/StepCard";
import { ColumnsView } from "./panes/ColumnsView";
import { RAIL, TRACE } from "./panes/columns";
import { useColumns, type Centre } from "./panes/useColumns";
import { useJobCatalog } from "./panes/catalog";
import { VIEWS } from "./panes/address";
import type { LinkTarget } from "./panes/drag";
import { nodeTarget, pageTarget } from "./lib/linkBase";
import type { EntityOpener } from "./lib/openEntity";
import { FrontendViewShellProvider } from "./views/FrontendViewShellProvider";
import type { LoadedViews } from "./views/registry";
import { ViewsNotice } from "./components/ViewsNotice";
import type { DispatchRegistry } from "fxtr-view-shell-util";
import { cx } from "./lib/cx";
import styles from "./App.module.css";
import { fetchJobPage, fetchJobSummary } from './api/graphql';
import { useQuery } from './api/browse';
import { onViewerEvent } from './api/events';

/** The run replay, on the dev server only: a production build leaves it, and its module, out. */
const RunReplay = import.meta.env.DEV ? lazy(() => import("./debug/RunReplay")) : null;

/** Space kept between the top of the scroller and a card scrolled to. */
const JUMP_MARGIN = 12;
/** The least of the workspace, in percent, the card column and the detail view each keep. In the
 *  trace the card column's is in pixels instead (below). */
const CARD_PANEL_MIN = 25;
const DETAIL_PANEL_MIN = 18;
/** The narrowest the trace's column can be dragged while a pane is open. */
const TRACE_COLLAPSED_WIDTH = 100;
/** The narrowest the trace is laid out: a narrower column leaves it this wide, under the detail
 *  view, dimmed, and one click away from being this wide again. */
const MIN_TRACE_CONTENT_WIDTH = 600;
/** The column width that shows the whole trace: a pixel over it, since panel sizes are rounded
 *  percentages and a column a fraction of a pixel narrower would leave the trace dimmed. */
const TRACE_EXPANDED_WIDTH = MIN_TRACE_CONTENT_WIDTH + 1;
/** The narrowest the workspace (everything right of the rail) is laid out: room for the trace and
 *  a detail view beside it. A narrower window scrolls the workspace sideways instead. */
const WORKSPACE_MIN_WIDTH = MIN_TRACE_CONTENT_WIDTH + 400;

/** Whether the card is on screen from edge to edge, inside the scroller's visible area. */
function fullyInView(scroller: HTMLElement, card: HTMLElement): boolean {
  const scrollBox = scroller.getBoundingClientRect();
  const cardBox = card.getBoundingClientRect();
  return (
    cardBox.top >= scrollBox.top &&
    cardBox.bottom <= scrollBox.top + scroller.clientHeight &&
    cardBox.left >= scrollBox.left &&
    cardBox.right <= scrollBox.left + scroller.clientWidth
  );
}

function scrollToCard(scroller: HTMLElement, card: HTMLElement) {
  const scrollBox = scroller.getBoundingClientRect();
  const cardBox = card.getBoundingClientRect();
  scroller.scrollTo({
    top: Math.max(
      0,
      scroller.scrollTop + cardBox.top - scrollBox.top - JUMP_MARGIN,
    ),
    behavior: "auto",
  });
}

export function App({ views }: { views: LoadedViews }) {
  return (
    <>
      <main className={styles.app}>
        <KeyboardShortcuts />
        <ErrorBoundary
          fallback={(error, reset) => <Problem error={error} onRetry={() => retryAndReset(reset)} />}
        >
          <Suspense fallback={<Loading what="loading jobs…" />}>
            <Jobs views={views} />
          </Suspense>
        </ErrorBoundary>
      </main>
      <Toasts />
    </>
  );
}

/** The job named in the URL hash, so a reload or a shared link lands on the same job, and changing
 *  it picks another. */
function jobFromHash(): string | null {
  const match = /(?:^#|&)job=([^&]+)/.exec(window.location.hash);
  return match ? decodeURIComponent(match[1]) : null;
}

function Jobs({ views }: { views: LoadedViews }) {
  const [jobId, setJobId] = useState<string | null>(
    () => jobFromHash(),
  );
  const query = useQuery(`job:${jobId}`, (signal) => jobId
    ? fetchJobSummary(jobId, signal)
    : fetchJobPage('', null, 1, signal).then((page) => page.items[0] ?? null));
  const [, startTransition] = useTransition();
  const job = query.value ?? null;
  const jobs = job ? [job] : [];
  useEffect(() => {
    if (jobId === null && job) setJobId(job.id);
  }, [jobId, job]);
  // Launches switch the page to their jobs until someone picks one here (lib/launches.tsx).
  const pick = useFollowLaunches(job?.id ?? null, (id) =>
    startTransition(() => setJobId(id)),
  );
  useEffect(() => {
    const picked = () => {
      const id = jobFromHash();
      if (id !== null) pick(id);
    };
    window.addEventListener("hashchange", picked);
    return () => window.removeEventListener("hashchange", picked);
  }, [pick]);
  useEffect(() => {
    const timer = window.setInterval(query.reload, 30_000);
    const stopFollowing = onViewerEvent((event) => {
      if (event.type === "job" && event.jobId === jobId) {
        query.reload();
        pokeLive();
      }
    });
    return () => {
      window.clearInterval(timer);
      stopFollowing();
    };
  }, [jobId, query.reload]);

  useEffect(() => {
    if (job)
      window.history.replaceState(
        null,
        "",
        `#job=${encodeURIComponent(job.id)}`,
      );
  }, [job]);

  // The job list may know of a run the records on screen do not (a job resumed elsewhere), so a
  // job listed as running is polled even if what is on screen looks settled.
  useEffect(() => {
    if (job?.state === "running") pokeLive();
  }, [job?.id, job?.state]);

  const picker = (
    <>
      <JobPicker jobs={jobs} selectedId={job?.id ?? null} onSelect={pick} />
      <LiveIndicator />
      <ViewsNotice problem={views.problem} />
    </>
  );
  if (!job)
    return (
      <div className={styles.body}>
        <aside className={styles.rail}>{picker}</aside>
          {query.error ? <Problem error={query.error instanceof Error ? query.error : new Error(String(query.error))} onRetry={query.reload} />
            : <Loading what={query.loading ? 'loading job…' : jobId ? 'job not found' : 'no jobs recorded'} />}
      </div>
    );
  return (
    <ErrorBoundary
      key={job.id}
      fallback={(error, reset) => (
        <div className={styles.body}>
          <aside className={styles.rail}>{picker}</aside>
          <Problem error={error} onRetry={() => retryAndReset(reset)} />
        </div>
      )}
    >
      <Suspense
        fallback={
          <div className={styles.body}>
            <aside className={styles.rail}>{picker}</aside>
            <Loading what="loading job…" />
          </div>
        }
      >
        <JobView job={job} picker={picker} registry={views.registry} />
      </Suspense>
    </ErrorBoundary>
  );
}

/** One job, shown one workflow invocation at a time: the root's DAG, or a child's opened from it. */
function JobView({
  job,
  picker,
  registry,
}: {
  job: JobSummary;
  picker: React.ReactNode;
  registry: DispatchRegistry;
}) {
  /** Every invocation opened below the root, outermost first; the breadcrumbs. */
  const [trail, setTrail] = useState<LevelRef[]>([]);
  /** Which breadcrumb is shown: 0 is the root, `k` is `trail[k - 1]`. */
  const [depth, setDepth] = useState(0);
  const pathname = depth > 0 ? trail[depth - 1].pathname : job.root;
  useLivePolling(job.id);
  // The root is always watched: it says whether the job is still live, whatever level is shown.
  useOverviewSnapshot(job.id, job.root);
  const loaded = useLevel(job.id, pathname);
  const level = loaded.steps;
  const result = loaded.result;
  const [, startTransition] = useTransition();

  // The returned value's table is indexed with the steps, so the right pane can show it too.
  const steps = useMemo(
    () => indexSteps(result?.table ? [...level, result.table] : level),
    [level, result],
  );
  const freshCards = useFreshIds(
    level.map((step) => step.id),
    pathname,
  );

  useEffect(() => {
    document.title = job.workflowName;
  }, [job.workflowName]);
  useTabIcon(loaded);
  const [selectedId, setSelectedId] = useState<string | null>(
    () => level[0]?.id ?? null,
  );
  /** The step a hovered chip has lit up: its minimap row, and its card when that is fully in view.
   *  A store, so a hover re-renders the row and card it lights, not the level. */
  const [spotlight] = useState(createSpotlightStore);
  const [minimapHoveredId, setMinimapHoveredId] = useState<string | null>(null);
  /** The card a chip last jumped the trace to, glowing until its glow fades; `seq` counts jumps,
   *  so jumping to the same card again replays the glow. */
  const [jumpGlow, setJumpGlow] = useState<{ id: string; seq: number } | null>(null);
  /** The slice: the value picked for each dim sliced on, shared by every card and pane, and kept
   *  across levels, since a dim's name means the same thing in each. */
  const [slice, setSlice] = useState<
    Readonly<Record<string, KeyValue>>
  >({});
  /** Each card's table state: a store, read by each card for its own. */
  const [cardTables] = useState(
    () => new Store<ReadonlyMap<string, CardTableState>>(new Map()),
  );
  /** The `InvocationPanel` renderer chosen to show the level in place of its trace, or null for
   *  the trace. */
  const [chosenPageView, setPageView] = useState<string | null>(null);
  /** The chosen renderer while the registry still has it: rebuilt project renderers may not. */
  const pageView = chosenRenderer(
    renderersFor(registry, InvocationPanel, loaded.workflowName),
    chosenPageView,
  );
  /** Whether the level is shown as a graph with one selected card instead of the trace list. */
  const [flowchartMode, setFlowchartMode] = useState(false);
  /** The level's own invocation as the one spawn a panel renderer of it is given. */
  const levelSpawn = useMemo<Spawn>(
    () => ({
      key: {},
      ref: { jobId: job.id, kind: "workflow", pathname },
      output: loaded.resultCid
        ? (new EntityID(loaded.resultCid) as EntityID<ArrayData>)
        : null,
    }),
    [job.id, pathname, loaded.resultCid],
  );
  const levelKey = invocationRefKey(levelSpawn.ref);
  const pageStorage = useMemo(
    () => atom<JsonValue>(null),
    [levelKey, pageView],
  );
  const workspaceRef = useRef<HTMLDivElement>(null);
  const cardAreaRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<HTMLElement>(null);
  const cardsRef = useRef<HTMLDivElement>(null);
  /** The card column's minimum size in percent: in the trace, `TRACE_COLLAPSED_WIDTH` while a
   *  panel is open and `TRACE_EXPANDED_WIDTH` while none is, so an empty detail view never lies
   *  over the trace. */
  const [cardPanelMin, setCardPanelMin] = useState(CARD_PANEL_MIN);
  const cardPanelRef = useRef<ImperativePanelHandle>(null);
  /** The trace's column is narrower than the trace, which lies partly under the detail view. */
  const [traceCollapsed, setTraceCollapsed] = useState(false);
  /** The columns are slid left over the trace (`ColumnsView`), which raises the detail view. */
  const [overTrace, setOverTrace] = useState(false);
  /** How far the columns are slid, as `--slide` on the slice control's overlay, which keeps clear
   *  of them: set on it alone rather than through state or on the workspace, since it changes every
   *  frame of a slide and a custom property set higher up restyles everything below. */
  const slideRef = useRef(0);
  const sliceOverlayRef = useRef<HTMLDivElement | null>(null);
  const onSlide = useCallback((slide: number) => {
    slideRef.current = slide;
    sliceOverlayRef.current?.style.setProperty("--slide", `${slide}px`);
    setOverTrace(slide > 0);
  }, []);
  /** The slice control's overlay, given the slide when it mounts. */
  const sliceOverlay = useCallback((element: HTMLDivElement | null) => {
    sliceOverlayRef.current = element;
    element?.style.setProperty("--slide", `${slideRef.current}px`);
  }, []);
  const [geometry, setGeometry] = useState<ReadonlyMap<string, CardGeometry>>(
    new Map(),
  );
  const [viewport] = useState(createViewportStore);
  const levelRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const cardRefs = useRef(new Map<string, HTMLElement>());
  /** A card to scroll to once the level it lives in has rendered. */
  const pendingJumpRef = useRef<string | null>(null);
  const holdCard = useScrollAnchor(scrollRef, cardsRef);
  /** The step names from the root workflow down to the level's: the start of its nodes' addresses. */
  const levelPath = useMemo(
    () => trail.slice(0, depth).map((ref) => ref.label.split("[")[0]!),
    [trail, depth],
  );
  /** What the panels' addresses resolve against: every level of the job, and the registry. */
  const catalog = useJobCatalog(job.id, job.root, registry);
  /** What the centre shows, as an address, and the keys of the workflows on the way to its level,
   *  which a link from the trace binds. */
  const centre = useMemo<Centre>(
    () => ({
      address: {
        view: pageView ?? (flowchartMode ? VIEWS.flowchart : VIEWS.trace),
        location: { root: "step", steps: levelPath, path: [] },
      },
      keys: Object.assign({}, ...trail.slice(0, depth).map((ref) => mappedKeyOf(ref.label))),
    }),
    [pageView, flowchartMode, levelPath, trail, depth],
  );
  /** The job's root workflow invocation, which pages are handed. */
  const run = useMemo<Spawn>(
    () => ({ key: {}, ref: { jobId: job.id, kind: "workflow", pathname: job.root }, output: null }),
    [job.id, job.root],
  );
  /** The detail view: columns of panels, opened by links. */
  const panes = useColumns(slice, catalog, centre);
  const traceSelected = useMemo(() => panes.selectedBelowOf(TRACE), [panes.selectedBelowOf]);
  const levelState = useMemo<LevelState>(
    () => ({
      slice,
      selected: traceSelected,
      levelPath,
      setSlice: (next, anchor) => {
        holdCard(anchor);
        setSlice(next);
      },
      cardTables,
    }),
    [slice, traceSelected, levelPath, cardTables, holdCard],
  );

  const selected =
    level.find((step) => step.id === selectedId) ?? level[0] ?? null;
  const dependencyFocus =
    level.find((step) => step.id === minimapHoveredId) ?? selected;
  const incoming = useMemo(() => {
    const visible = new Set(level.map((step) => step.id));
    return (dependencyFocus?.deps ?? []).filter((id) => visible.has(id));
  }, [dependencyFocus, level]);
  /** Links outside any panel open from a root of the viewer: the trace's from the trace, the
   *  rail's (the experiment, pages) from the rail. */
  const openFrom = useCallback(
    (target: LinkTarget, root: string, pin = false) => panes.follow(target, root, { pin }),
    [panes.follow],
  );
  const openExperimentReadme = () => openFrom(pageTarget(VIEWS.experimentReadme), RAIL);
  const entityOpener = useMemo<EntityOpener>(
    () => ({
      originId: TRACE,
      viewId: centre.address.view,
      relative: true,
      follow: (target, options) => panes.follow(target, TRACE, options),
      hover: (target) => panes.hover(target, TRACE),
      linkState: (target) => panes.linkState(target, TRACE),
    }),
    [centre.address.view, panes.follow, panes.hover, panes.linkState],
  );

  const cardSpotlight = useMemo<CardSpotlight>(
    () => ({
      light: (id) => {
        const card = cardRefs.current.get(id);
        const scroller = scrollRef.current;
        const inView = !!card && !!scroller && fullyInView(scroller, card);
        spotlight.set({ id, card: inView });
        return inView;
      },
      clear: (id) => spotlight.update((current) => (current?.id === id ? null : current)),
      spotlight,
    }),
    [spotlight],
  );

  // Card positions feed the minimap's viewport indicator, so they are re-measured on any resize.
  useLayoutEffect(() => {
    const root = levelRef.current;
    if (!root) return;
    const measure = () => {
      const rootBox = root.getBoundingClientRect();
      const next = new Map<string, CardGeometry>();
      for (const step of level) {
        const card = cardRefs.current.get(step.id);
        const head = card?.querySelector<HTMLElement>("[data-card-head]");
        if (!card || !head) continue;
        const cardBox = card.getBoundingClientRect();
        const headBox = head.getBoundingClientRect();
        next.set(step.id, {
          top: cardBox.top - rootBox.top,
          bottom: cardBox.bottom - rootBox.top,
          center: headBox.top - rootBox.top + headBox.height / 2,
        });
      }
      setGeometry(next);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    for (const card of cardRefs.current.values()) observer.observe(card);
    measure();
    return () => observer.disconnect();
  }, [level]);

  // The workspace's geometry as CSS variables, set on the element rather than through state so
  // dragging the divider does not re-render the level: the width the cards are laid out in, the
  // width of the scroller's scrollbar, and where the detail view starts. It also sets the card column's
  // minimum size and whether the trace is collapsed, which are state, but change only with the
  // workspace's width and as the divider crosses `MIN_TRACE_CONTENT_WIDTH`.
  useLayoutEffect(() => {
    const workspace = workspaceRef.current;
    const detail = detailRef.current;
    if (!workspace || !detail) return;
    const measure = () => {
      const workspaceBox = workspace.getBoundingClientRect();
      const detailLeft = detail.getBoundingClientRect().left - workspaceBox.left;
      workspace.style.setProperty("--detail-left", `${detailLeft}px`);
      workspace.style.setProperty("--trace-content-min", `${MIN_TRACE_CONTENT_WIDTH}px`);
      const cardArea = cardAreaRef.current;
      const cardWidth = cardArea?.getBoundingClientRect().width;
      if (cardWidth !== undefined)
        workspace.style.setProperty("--card-width", `${cardWidth}px`);
      const scroller = scrollRef.current;
      const scrollbar = scroller ? scroller.offsetWidth - scroller.clientWidth : 0;
      workspace.style.setProperty("--scrollbar-width", `${scrollbar}px`);
      const traceMin = flowchartMode
        ? null
        : panelPercent(panes.anyOpen ? TRACE_COLLAPSED_WIDTH : TRACE_EXPANDED_WIDTH);
      setCardPanelMin(traceMin === null ? CARD_PANEL_MIN : Math.min(100 - DETAIL_PANEL_MIN, traceMin));
      setTraceCollapsed(
        !flowchartMode && cardWidth !== undefined && cardWidth < MIN_TRACE_CONTENT_WIDTH,
      );
    };
    const observer = new ResizeObserver(measure);
    for (const element of [
      workspace,
      detail,
      cardAreaRef.current,
      cardsRef.current,
      scrollRef.current,
    ])
      if (element) observer.observe(element);
    measure();
    return () => observer.disconnect();
  }, [flowchartMode, pageView, pathname, panes.anyOpen]);

  /** A width as a percentage of the panels' (the workspace less the resize handle), the unit panel
   *  sizes are in; null outside the trace, where the card column is not measured. */
  function panelPercent(width: number): number | null {
    const workspace = workspaceRef.current;
    const detail = detailRef.current;
    const cardArea = cardAreaRef.current;
    if (!workspace || !detail || !cardArea) return null;
    const workspaceBox = workspace.getBoundingClientRect();
    const handle =
      detail.getBoundingClientRect().left - workspaceBox.left - cardArea.getBoundingClientRect().width;
    const panelsWidth = workspaceBox.width - handle;
    return panelsWidth > 0 ? (width / panelsWidth) * 100 : null;
  }

  /** Widen a collapsed trace's column back to the trace's own width. */
  const expandTrace = () => {
    const size = panelPercent(TRACE_EXPANDED_WIDTH);
    if (size !== null) cardPanelRef.current?.resize(Math.min(100 - DETAIL_PANEL_MIN, size));
  };

  // Only showing another level starts the trace at the top; a live update keeps the scroll.
  useLayoutEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [pathname]);

  useLayoutEffect(() => {
    const id = pendingJumpRef.current;
    const card = id ? cardRefs.current.get(id) : null;
    if (!card || !scrollRef.current) return;
    scrollToCard(scrollRef.current, card);
    pendingJumpRef.current = null;
  }, [flowchartMode, level, selectedId]);

  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const measure = () =>
      viewport.set({ top: scroller.scrollTop, height: scroller.clientHeight });
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    scroller.addEventListener("scroll", measure, { passive: true });
    measure();
    return () => {
      observer.disconnect();
      scroller.removeEventListener("scroll", measure);
    };
  }, [pathname, flowchartMode, viewport]);

  /** Show another invocation's DAG; the current one stays up while the new one loads. */
  const showLevel = (nextTrail: LevelRef[], nextDepth: number) => {
    startTransition(() => {
      setTrail(nextTrail);
      setDepth(nextDepth);
      setSelectedId(null);
      setMinimapHoveredId(null);
      setJumpGlow(null);
      setPageView(null);
      cardTables.set(new Map());
    });
  };

  /** Show the level of the workflow invocation at `target` in the trace: the breadcrumbs kept when
   *  it is among them (so the levels below it stay), else the levels down to it. */
  const openPathname = (target: string) => {
    const next = levelTrail(job.root, target);
    const kept = next.every((ref, index) => trail[index]?.pathname === ref.pathname);
    showLevel(kept ? trail : next, next.length);
  };

  const jumpToCard = (id: string) => {
    const card = cardRefs.current.get(id);
    if (card && scrollRef.current) scrollToCard(scrollRef.current, card);
  };

  /** Select a step of this level (arguments only ever name steps of the same invocation). */
  const selectStep = (id: string) => {
    if (!flowchartMode) {
      jumpToCard(id);
      setJumpGlow((current) => ({ id, seq: (current?.seq ?? 0) + 1 }));
    }
    setSelectedId(id);
  };

  // Cards are memoized, so the level's own state (a jump's glow, the minimap's
  // hover, card geometry) re-renders only the ones it changes; their callbacks keep one identity.
  const endJumpGlow = useCallback(
    (id: string) => setJumpGlow((current) => (current?.id === id ? null : current)),
    [],
  );
  const registerCard = useCallback((id: string, element: HTMLElement | null) => {
    if (element) cardRefs.current.set(id, element);
    else cardRefs.current.delete(id);
  }, []);
  const stableOpenPathname = useStableCallback(openPathname);
  const levelOpener = useMemo<LevelOpener>(
    () => ({ open: stableOpenPathname, shown: pathname }),
    [stableOpenPathname, pathname],
  );
  const stableSelectStep = useStableCallback(selectStep);

  const renderStepCard = (step: (typeof level)[number], inTrace: boolean) => (
    <StepCard
      // Node identities are content-addressed, so the same one can appear in two levels; the
      // pathname keeps a card's state from carrying over.
      key={`${pathname}:${step.id}`}
      step={step}
      steps={steps}
      jumpGlow={inTrace && jumpGlow?.id === step.id ? jumpGlow.seq : null}
      onJumpGlowEnd={endJumpGlow}
      fresh={inTrace && freshCards.has(step.id)}
      edgeToEdge={!inTrace}
      viewsShown={panes.viewsShownOf([...levelPath, step.name])}
      onCardElement={inTrace ? registerCard : undefined}
      onActivate={setSelectedId}
      onSelectStep={stableSelectStep}
    />
  );

  return (
    <FrontendViewShellProvider registry={registry}>
      <LevelStateContext.Provider value={levelState}>
      <OpenLevelContext.Provider value={levelOpener}>
      <div className={styles.body}>
        {!flowchartMode && (
          <aside className={styles.rail}>
            {picker}
            {/* Below the job selector and above the minimap's breadcrumbs; renders nothing when the
                project has no experiment. */}
            <ExperimentReadmeLink
              onOpen={openExperimentReadme}
              active={panes.showsExperimentReadme}
            />
            <Minimap
              rootLabel={job.root}
              trail={trail}
              depth={depth}
              level={level}
              spotlight={pageView === null ? spotlight : null}
              focus={pageView === null ? dependencyFocus : null}
              incoming={pageView === null ? incoming : []}
              hovering={pageView === null && minimapHoveredId !== null}
              geometry={geometry}
              viewport={viewport}
              onShowLevel={(nextDepth) => showLevel(trail, nextDepth)}
              onClearBelow={(nextDepth) =>
                showLevel(trail.slice(0, nextDepth), nextDepth)
              }
              onHover={setMinimapHoveredId}
              onOpenFlowchart={() => {
                setPageView(null);
                setFlowchartMode(true);
              }}
              // A row picked while a renderer shows the level returns to the trace, where the
              // card is; it is scrolled to once the cards have rendered.
              onJump={(id) => {
                setSelectedId(id);
                if (pageView !== null) {
                  setPageView(null);
                  pendingJumpRef.current = id;
                } else jumpToCard(id);
              }}
            >
              <LevelViews
                workflowName={loaded.workflowName}
                active={pageView}
                onChange={setPageView}
              />
            </Minimap>
            {RunReplay && (
              <Suspense fallback={null}>
                <RunReplay jobId={job.id} />
              </Suspense>
            )}
            <ThemeToggle />
          </aside>
        )}
        <div className={styles.workspaceScroll}>
        <div className={styles.workspace} ref={workspaceRef} style={{ minWidth: WORKSPACE_MIN_WIDTH }}>
        <PanelGroup direction="horizontal" className={styles.panels}>
          <Panel ref={cardPanelRef} defaultSize={50} minSize={cardPanelMin}>
            {flowchartMode ? (
              <div className={styles.flowchartMode}>
                <div className={styles.flowchartControls}>
                  {picker}
                  <ExperimentReadmeLink
                    onOpen={openExperimentReadme}
                    active={panes.showsExperimentReadme}
                  />
                  <WorkflowBreadcrumbs
                    rootLabel={job.root}
                    trail={trail}
                    depth={depth}
                    onShowLevel={(nextDepth) => showLevel(trail, nextDepth)}
                    onClearBelow={(nextDepth) =>
                      showLevel(trail.slice(0, nextDepth), nextDepth)
                    }
                  />
                  <div className={styles.flowchartToggleRow}>
                    <span>flowchart</span>
                    <FlowchartToggle
                      active
                      onClick={() => {
                        pendingJumpRef.current = selected?.id ?? null;
                        setFlowchartMode(false);
                      }}
                    />
                  </div>
                  <LevelViews
                    workflowName={loaded.workflowName}
                    active={pageView}
                    onChange={(renderer) => {
                      setPageView(renderer);
                      if (renderer !== null) setFlowchartMode(false);
                    }}
                  />
                </div>
                <SliceDims steps={level} className={styles.flowchartSlice} />
                <PanelGroup direction="vertical" className={styles.flowchartSplit}>
                  <Panel defaultSize={62} minSize={30}>
                    <Flowchart
                      levelKey={pathname}
                      level={level}
                      selectedId={selected?.id ?? null}
                      onSelect={(id) => {
                        setSelectedId(id);
                        openFrom(nodeTarget(levelPath, steps.get(id)!), TRACE);
                      }}
                    />
                  </Panel>
                  <PanelResizeHandle className={styles.flowchartHandle} />
                  <Panel defaultSize={38} minSize={20}>
                    <div className={styles.flowchartCardScroll}>
                      <div className={styles.flowchartCard}>
                        <OpenEntityContext.Provider value={entityOpener}>
                          {selected
                            ? renderStepCard(selected, false)
                            : <Loading what="this invocation recorded no nodes" />}
                        </OpenEntityContext.Provider>
                      </div>
                    </div>
                  </Panel>
                </PanelGroup>
              </div>
            ) : (
              // The trace scrolls in an overlay over the whole workspace (below); this only
              // measures the width the cards are laid out in.
              <div className={styles.cardArea} ref={cardAreaRef} />
            )}
          </Panel>
          <PanelResizeHandle className={`handle ${styles.workspaceHandle}`} />
          <Panel
            defaultSize={50}
            minSize={DETAIL_PANEL_MIN}
            className={cx((traceCollapsed || overTrace) && styles.isOverTrace)}
            // The columns slide left past the panel's edge, over the trace.
            style={{ overflow: "visible" }}
          >
            <aside className={styles.detailPane} ref={detailRef}>
              <ColumnsView
                state={panes}
                onSlide={onSlide}
                renderPanel={(panel) => (
                  <DetailPane jobId={job.id} panel={panel} columns={panes} run={run} />
                )}
              />
            </aside>
          </Panel>
        </PanelGroup>
        {!flowchartMode && (
          <div
            ref={scrollRef}
            className={cx(
              styles.scroll,
              traceCollapsed && styles.isCollapsed,
            )}
            // A collapsed trace is inert (see `.is-collapsed`), so a click anywhere lands here.
            onClick={traceCollapsed ? expandTrace : undefined}
          >
                <div className={styles.levelColumn}>
                {pageView === null && (
                  <div className={styles.runningBar}>
                    <RunningIndicator
                      level={level}
                      geometry={geometry}
                      viewport={viewport}
                      onJump={stableSelectStep}
                    />
                  </div>
                )}
                <div className={cx(styles.level, pageView === null && styles.hasSliceBar)} ref={levelRef}>
                  <OpenEntityContext.Provider value={entityOpener}>
                  <CardSpotlightContext.Provider value={cardSpotlight}>
                    {pageView !== null ? (
                      <RenderInvocation
                        slot={InvocationPanel}
                        name={loaded.workflowName}
                        renderer={pageView}
                        invocations={[levelSpawn]}
                        storage={pageStorage}
                      />
                    ) : (
                      <>
                        <LevelHeader
                          level={loaded}
                          steps={steps}
                          onSelectStep={selectStep}
                        />
                        <div className={styles.cards} ref={cardsRef}>
                          {level.map((step) => (
                            <div key={`${pathname}:${step.id}`} className={styles.cardRow} data-card-id={step.id}>
                              {renderStepCard(step, true)}
                            </div>
                          ))}
                          {level.length === 0 && (
                            <Loading what="this invocation recorded no nodes" />
                          )}
                          <div key={`${pathname}:return`} className={styles.cardRow} data-card-id={result?.table?.id}>
                            <ReturnCard
                              level={loaded}
                              steps={steps}
                              valueShown={
                                result?.table ? panes.showsReturnOf(levelPath) : false
                              }
                              onSelectStep={selectStep}
                            />
                          </div>
                        </div>
                      </>
                    )}
                  </CardSpotlightContext.Provider>
                  </OpenEntityContext.Provider>
                </div>
              </div>
          </div>
        )}
        {!flowchartMode && pageView === null && (
          // Over the workspace rather than in the trace, so the columns sliding over the trace push
          // it left instead of covering it, until it stops at the trace's left edge.
          <div className={styles.sliceOverlay} ref={sliceOverlay}>
            <SliceDims steps={level} panelAlign="left" />
          </div>
        )}
        </div>
        </div>
      </div>
      </OpenLevelContext.Provider>
      </LevelStateContext.Provider>
    </FrontendViewShellProvider>
  );
}
