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
import type { JobSummary, LevelRef } from "./types";
import { DetailPane } from "./components/DetailPane";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Flowchart } from "./components/Flowchart";
import { SliceDims } from "./components/SliceDims";
import { RunningIndicator } from "./components/RunningIndicator";
import { JobPicker } from "./components/JobPicker";
import { Toasts } from "./components/Toasts";
import { ViewerSettings } from "./components/ViewerSettings";
import { KeyboardShortcuts } from "./components/KeyboardShortcuts";
import { LevelHeader } from "./components/LevelHeader";
import { LiveIndicator } from "./components/LiveIndicator";
import { LevelViews } from "./components/LevelViews";
import { ExperimentReadmeLink } from "./components/ExperimentReadme";
import {
  FlowchartToggle,
  Minimap,
  WorkflowBreadcrumbs,
  type CardGeometry,
  createViewportStore,
} from "./components/Minimap";
import { ReturnCard } from "./components/ReturnCard";
import { Loading, Nothing, Problem, Shown } from "./components/Status";
import { StepCard } from "./components/StepCard";
import { ColumnsView } from "./panes/ColumnsView";
import { RAIL, TRACE } from "./panes/columns";
import { useColumns, type Center } from "./panes/useColumns";
import { levelAt } from "./panes/resolve";
import {
  centerView,
  EMPTY_URL_STATE,
  formatUrlState,
  pageViewFrom,
  parseUrlState,
  snapshotState,
  urlPanelsOf,
  type PageView,
  type UrlWorkflow,
  type ViewState,
} from "./lib/urlState";
import { UrlWriter } from "./lib/urlWriter";
import { showLinkProblem, showLinkProblems } from "./lib/linkProblem";
import { useJobCatalog } from "./panes/catalog";
import { VIEWS, formatAddress, type Params } from "./panes/address";
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
import { keepQueryValue } from './api/useQuery';
import { onViewerEvent } from './api/events';

/** The run replay, on the dev server only: a production build leaves it, and its module, out. */
const RunReplay = import.meta.env.DEV ? lazy(() => import("./debug/RunReplay")) : null;

/** Space kept between the top of the scroller and a card scrolled to: enough to clear the slice
 *  control floating there (`.has-slice-bar`), which would otherwise cover the card's views button. */
const JUMP_MARGIN = 46;
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
    cardBox.top >= scrollBox.top + JUMP_MARGIN &&
    cardBox.bottom <= scrollBox.top + scroller.clientHeight &&
    cardBox.left >= scrollBox.left &&
    cardBox.right <= scrollBox.left + scroller.clientWidth
  );
}

/** The name a link gives the return card (`card=return`): no step can be called `return`, a
 *  keyword in Python. */
const RETURN_CARD = "return";

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
          <Suspense fallback={<div className={styles.body}><Loading what="Opening the job…" /></div>}>
            <Jobs views={views} />
          </Suspense>
        </ErrorBoundary>
      </main>
      <ViewerSettings />
      <Toasts />
    </>
  );
}

/** The job named in the URL hash, so a reload or a shared link lands on the same job, and changing
 *  it (back and forward among them) picks another. */
function jobFromHash(): string | null {
  return parseUrlState(window.location.hash).state.job;
}

const jobSummaryKey = (jobId: string | null) => `job:${jobId}`;

/** A summary the job picker has read, kept as the page's read of it: a job opened from the picker
 *  shows without waiting for its summary, which is then read afresh. */
function keepJobSummary(summary: JobSummary) {
  keepQueryValue(jobSummaryKey(summary.id), summary);
}

function Jobs({ views }: { views: LoadedViews }) {
  const [jobId, setJobId] = useState<string | null>(
    () => jobFromHash(),
  );
  const query = useQuery(jobSummaryKey(jobId), (signal) => jobId
    ? fetchJobSummary(jobId, signal)
    : fetchJobPage('', null, 1, signal).then((page) => page.items[0] ?? null));
  const job = query.value ?? null;
  /** A link naming no job was come to by back, forward, or editing the hash, and the newest job is
   *  being looked up for it; the number of the lookup, so only the last one counts. */
  const [findingNewest, setFindingNewest] = useState<number | null>(null);
  /** The job loaded is the one asked for: not the last one's summary kept while the next one's
   *  loads, nor while the newest is being looked up. Until it is, the page shows that it is loading
   *  rather than the job before, and writes nothing, since the URL is already the next job's. */
  const asked = findingNewest === null && (jobId === null || job === null || job.id === jobId);
  const jobs = job ? [job] : [];
  useEffect(() => {
    if (jobId === null && job) setJobId(job.id);
  }, [jobId, job]);
  // Launches switch the page to their jobs until someone picks one here (lib/launches.tsx).
  // Not in a transition: one would keep the job before on screen until the next one has loaded.
  const pick = useFollowLaunches(job?.id ?? null, setJobId);
  /** The one writer of the URL and the history (`lib/urlWriter.ts`): this says which job is shown,
   *  the job's view what it shows. */
  const [urlWriter] = useState(() => new UrlWriter());
  useEffect(() => () => urlWriter.dispose(), [urlWriter]);
  useEffect(() => {
    if (job && asked) urlWriter.showJob(job.id);
  }, [job?.id, asked, urlWriter]);

  /** Counts the history entries come to by back, forward, or an edited link: the job's view is
   *  opened afresh from each (`JobView` reads the entry as it opens). */
  const [entry, setEntry] = useState(0);
  // Back, forward, or an edited link picks the job it names (the one shown, too: someone chose to
  // stay on it), and opens its view afresh from the entry.
  // An edited link fires both events: only `popstate` opens the view afresh, once.
  useEffect(() => {
    const picked = () => {
      const id = jobFromHash();
      if (id !== null) pick(id);
    };
    let lookups = 0;
    const moved = () => {
      urlWriter.popped();
      const id = jobFromHash();
      lookups += 1;
      if (id === null) {
        // A link naming no job means the newest, as on loading the page.
        const lookup = lookups;
        setFindingNewest(lookup);
        fetchJobPage("", null, 1).then(
          (page) => {
            if (lookup !== lookups) return;
            const newest = page.items[0];
            setFindingNewest(null);
            setEntry((current) => current + 1);
            if (newest) pick(newest.id);
          },
          () => {
            if (lookup === lookups) setFindingNewest(null);
          },
        );
        return;
      }
      // In the render the pick is in, so the view does not open afresh before the job asked for
      // is known: the one shown would open from the next one's entry.
      setFindingNewest(null);
      setEntry((current) => current + 1);
      pick(id);
    };
    window.addEventListener("popstate", moved);
    window.addEventListener("hashchange", picked);
    return () => {
      window.removeEventListener("popstate", moved);
      window.removeEventListener("hashchange", picked);
    };
  }, [pick, urlWriter]);
  // A resume or sync may finish between refreshes, so settled records must refresh too.
  useEffect(() => {
    const refresh = () => {
      query.reload();
      pokeLive();
    };
    const timer = window.setInterval(refresh, 30_000);
    const stopFollowing = onViewerEvent((event) => {
      if (event.type === "job" && event.jobId === jobId) {
        refresh();
      }
    });
    return () => {
      window.clearInterval(timer);
      stopFollowing();
    };
  }, [jobId, query.reload]);

  // The job list may know of a run the records on screen do not (a job resumed elsewhere), so a
  // job listed as running is polled even if what is on screen looks settled.
  useEffect(() => {
    if (job?.state === "running") pokeLive();
  }, [job?.id, job?.state]);

  const picker = (
    <>
      <JobPicker
        jobs={jobs}
        selectedId={job?.id ?? null}
        onSelect={(id, summary) => {
          if (summary) keepJobSummary(summary);
          pick(id);
        }}
        // Opened as a link to the step's card is, so the job's view opens afresh at it.
        onOpenStep={(id, card, summary) => {
          if (summary) keepJobSummary(summary);
          window.location.hash = formatUrlState({ ...EMPTY_URL_STATE, job: id, card });
        }}
      />
      <LiveIndicator />
      <ViewsNotice problem={views.problem} />
    </>
  );
  // The view opens afresh for each entry come to, once its job is the one asked for, with what
  // the entry says to show of the job: worked out once per entry, and kept in an object this owns
  // from its first render, which renders thrown away (the view suspending, and
  // retried) do not undo, as they would a memo; so every retry opens with the same panels.
  const [view] = useState(() => ({ key: "", opening: null as PageView | null }));
  if (job && asked && view.key !== `${job.id}:${entry}`) {
    view.key = `${job.id}:${entry}`;
    view.opening = pageViewFrom(job.id, window.location.hash, window.history.state, window.innerHeight);
  }
  const viewKey = view.key;
  const opening = view.opening;
  // While the job opens there is nothing to choose or browse, so its message has the whole space.
  if (!job && query.loading)
    return (
      <div className={styles.body}>
        <Loading what="Opening the job…" />
      </div>
    );
  if (!job || !asked)
    return (
      <div className={styles.body}>
        <aside className={styles.rail}>{picker}</aside>
          {query.error ? <Problem error={query.error instanceof Error ? query.error : new Error(String(query.error))} onRetry={query.reload} />
            : job ? <Loading what="Opening the job…" />
            : <Nothing what={jobId ? 'job not found' : 'no jobs recorded'} />}
      </div>
    );
  return (
    <ErrorBoundary
      key={viewKey}
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
            <Loading what="Opening the job…" />
          </div>
        }
      >
        <JobView job={job} opening={opening} picker={picker} registry={views.registry} urlWriter={urlWriter} />
        <Shown />
      </Suspense>
    </ErrorBoundary>
  );
}

/** One job, shown one workflow invocation at a time: the root's DAG, or a child's opened from it. */
function JobView({
  job,
  opening,
  picker,
  registry,
  urlWriter,
}: {
  job: JobSummary;
  opening: PageView | null;
  picker: React.ReactNode;
  registry: DispatchRegistry;
  urlWriter: UrlWriter;
}) {
  /** What the URL, or the history entry, says to show of this job as it opens (`lib/urlState.ts`);
   *  null when it names another (a job switched to starts afresh). */
  const [initial] = useState(opening);
  /** Every invocation opened below the root, outermost first; the breadcrumbs. */
  const [trail, setTrail] = useState<readonly LevelRef[]>(initial?.view.trail ?? []);
  /** Which breadcrumb is shown: 0 is the root, `k` is `trail[k - 1]`. */
  const [depth, setDepth] = useState(initial?.view.depth ?? 0);
  /** A link's center at a level below the root, until the levels on the way load and say which
   *  invocation it is. */
  const [pendingCenter, setPendingCenter] = useState<UrlWorkflow | null>(initial?.pending ?? null);
  /** The card a link names (`card` in the URL), kept until the level changes; `pending` until it
   *  has been gone to, once its level is shown and the card is in it. */
  const [linkedCard, setLinkedCard] = useState<{ name: string; pending: boolean } | null>(
    initial?.view.card ? { name: initial.view.card, pending: true } : null,
  );
  const linkCard = (name: string | null) => setLinkedCard(name === null ? null : { name, pending: true });
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
  const [slice, setSlice] = useState<Params>(initial?.view.slice ?? {});
  /** Each card's table state: a store, read by each card for its own. */
  const [cardTables] = useState(
    () => new Store<ReadonlyMap<string, CardTableState>>(new Map()),
  );
  /** The `InvocationPanel` renderer chosen to show the level in place of its trace, or null for
   *  the trace. */
  const [chosenPageView, setPageView] = useState<string | null>(initial?.view.pageView ?? null);
  /** The chosen renderer while the registry still has it: rebuilt project renderers may not. */
  const pageView = chosenRenderer(
    renderersFor(registry, InvocationPanel, loaded.workflowName),
    chosenPageView,
  );
  /** Whether the level is shown as a graph with one selected card instead of the trace list. */
  const [flowchartMode, setFlowchartMode] = useState(initial?.view.flowchartMode ?? false);
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
  /** What the center shows, as an address, and the keys of the workflows on the way to its level,
   *  which a link from the trace binds. */
  const center = useMemo<Center>(
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
  const panes = useColumns(
    slice,
    catalog,
    center,
    initial ? { columns: initial.view.columns, check: initial.check } : undefined,
  );
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
      viewId: center.address.view,
      relative: true,
      follow: (target, options) => panes.follow(target, TRACE, options),
      hover: (target) => panes.hover(target, TRACE),
      linkState: (target) => panes.linkState(target, TRACE),
    }),
    [center.address.view, panes.follow, panes.hover, panes.linkState],
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
    // The return card is not a step card, so it is found by its row.
    const card = id === null ? null
      : id === RETURN_CARD ? cardsRef.current?.querySelector<HTMLElement>("[data-return-card]")
      : cardRefs.current.get(id);
    if (!card || !scrollRef.current) return;
    scrollToCard(scrollRef.current, card);
    pendingJumpRef.current = null;
    // `jumpGlow` changes with each jump, even to the card already selected.
  }, [flowchartMode, level, selectedId, jumpGlow]);

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

  /** Show another invocation's DAG (in the trace, unless `view` says otherwise); the current one
   *  stays up while the new one loads. */
  const showLevel = (
    nextTrail: readonly LevelRef[],
    nextDepth: number,
    view: { pageView: string | null; flowchartMode?: boolean; card?: string | null } = { pageView: null },
  ) => {
    startTransition(() => {
      setTrail(nextTrail);
      setDepth(nextDepth);
      linkCard(view.card ?? null);
      setPageView(view.pageView);
      if (view.flowchartMode !== undefined) setFlowchartMode(view.flowchartMode);
      forgetLevel();
    });
  };

  /** Forget what belongs to the level shown, as another is shown: the selected card, hovers, a
   *  jump's glow, and the cards' tables. */
  const forgetLevel = () => {
    setSelectedId(null);
    setMinimapHoveredId(null);
    setJumpGlow(null);
    cardTables.set(new Map());
  };

  // A link's center below the root is shown once the levels on the way say which invocation it is.
  useEffect(() => {
    if (pendingCenter === null) return;
    const steps = pendingCenter.address.location?.root === "step" ? pendingCenter.address.location.steps : [];
    const place = levelAt(steps, pendingCenter.keys, catalog);
    if (place.status === "loading") return;
    if (place.status === "problem") {
      setPendingCenter(null);
      showLinkProblem(formatAddress(pendingCenter.address, pendingCenter.keys), place.problem);
      return;
    }
    const nextTrail = levelTrail(job.root, place.pathname);
    // In one transition with the level, so the URL is next written with the level shown.
    startTransition(() => {
      setPendingCenter(null);
      showLevel(nextTrail, nextTrail.length, { ...centerView(pendingCenter.address.view), card: linkedCard?.name ?? null });
    });
    // `showLevel` is the latest render's; what it sets does not depend on which.
  }, [pendingCenter, catalog, job.root]);

  // A linked card is scrolled to (the trace) or selected (the flowchart) once its level is shown and
  // the card is in it: a step still to appear in a running level is waited for.
  useEffect(() => {
    if (!linkedCard?.pending || pendingCenter !== null) return;
    const { name } = linkedCard;
    if (pageView !== null) {
      setLinkedCard(null);
      showLinkProblems([`card=${name}: the level is shown by ${pageView}, which has no cards`]);
      return;
    }
    if (name === RETURN_CARD && !flowchartMode) {
      // The trace always has the return card, whatever it holds yet.
      setLinkedCard({ name, pending: false });
      pendingJumpRef.current = RETURN_CARD;
      setJumpGlow((current) => ({ id: RETURN_CARD, seq: (current?.seq ?? 0) + 1 }));
      return;
    }
    // In the flowchart, `return` selects what was returned: its own table, or the step returned.
    const id = name === RETURN_CARD
      ? result?.table?.id ?? result?.stepId ?? undefined
      : level.find((step) => step.name === name)?.id;
    if (id === undefined) {
      if (loaded.live) return;
      setLinkedCard(null);
      showLinkProblems([name === RETURN_CARD
        ? `card=${name}: this level returned nothing to select in the flowchart`
        : `card=${name}: this level has no step ${name}`]);
      return;
    }
    setLinkedCard({ name, pending: false });
    setSelectedId(id);
    if (!flowchartMode) {
      pendingJumpRef.current = id;
      setJumpGlow((current) => ({ id, seq: (current?.seq ?? 0) + 1 }));
    }
  }, [linkedCard, pendingCenter, pageView, level, result, loaded.live, flowchartMode]);

  // What in the link did not read, once.
  useEffect(() => {
    if (initial && initial.problems.length > 0) showLinkProblems(initial.problems);
  }, [initial]);

  // The URL and the history entry follow what the page shows (`lib/urlWriter.ts`).
  const viewState = useMemo<ViewState>(
    () => ({
      columns: panes.columns, slice, trail, depth, pageView: chosenPageView, flowchartMode, card: linkedCard?.name ?? null,
    }),
    [panes.columns, slice, trail, depth, chosenPageView, flowchartMode, linkedCard?.name],
  );
  const urlHash = useMemo(() => {
    const atRoot = center.address.view === VIEWS.trace && levelPath.length === 0;
    return formatUrlState({
      job: job.id,
      workflow: atRoot ? null : { address: center.address, keys: center.keys },
      slice: viewState.slice,
      card: viewState.card,
      ...urlPanelsOf(viewState.columns),
    });
  }, [job.id, center, levelPath, viewState]);
  const snapshot = useMemo(() => snapshotState(job.id, viewState), [job.id, viewState]);
  // Nothing is written while a link is still being worked out (its level found, its panels
  // checked): the entry keeps the link, so a reload or another opening of it works it out again.
  const linkPending = pendingCenter !== null || panes.checking;
  useEffect(() => {
    if (!linkPending) urlWriter.show(job.id, urlHash, snapshot);
  }, [urlWriter, job.id, urlHash, snapshot, linkPending]);


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
                            : <Nothing what="this invocation recorded no nodes" />}
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
                            <Nothing what="this invocation recorded no nodes" />
                          )}
                          <div key={`${pathname}:return`} className={styles.cardRow} data-card-id={result?.table?.id} data-return-card>
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
