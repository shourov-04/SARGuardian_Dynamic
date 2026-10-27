import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { onAuthStateChanged, signInWithPopup, signOut, type User } from 'firebase/auth';
import type { Engine } from './data/engineData';
import { getDisplacementJob, listDisplacementJobs, type DisplacementJob } from './api/catalog';
import { Home } from './pages/Home';
import { LandslidePage } from '../landslide/ui/LandslidePage';
import { isLangtangExplorerEnabled } from '../landslide/langtang/feature';
import { canOpenLangtangExplorer, resolveLangtangRouteContext, visualizationForJob } from '../landslide/langtang/availability';
import { auth, firebaseConfigured, googleProvider, missingFirebaseConfig } from './firebase';
import { isLandslideDisplacementRoute, isLandslideLangtangRoute, isLandslideResultsRoute, isLandslideRoute, isLandslideVisualizationRoute, LANDSLIDE_DISPLACEMENT_PATH, LANDSLIDE_LANGTANG_PATH, LANDSLIDE_PATH, LANDSLIDE_RESULT_PATH, LANDSLIDE_RESULTS_PATH, navigatePath, normalizePathname } from './routing';

const LangtangExplorerPage = lazy(() => import('../landslide/langtang/LangtangExplorerPage')
  .then((module) => ({ default: module.LangtangExplorerPage })));
const JobResultExplorerPage = lazy(() => import('../landslide/langtang/JobResultExplorerPage')
  .then((module) => ({ default: module.JobResultExplorerPage })));

function App() {
  const [pathname, setPathname] = useState(() => normalizePathname(window.location.pathname));
  const pathnameRef = useRef(pathname);
  const [user, setUser] = useState<User | null>(null);
  const [authState, setAuthState] = useState<'idle' | 'loading' | 'ready' | 'error'>('loading');
  const [authError, setAuthError] = useState<string | null>(null);
  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [pendingRoute, setPendingRoute] = useState<string | null>(null);
  const [hasActiveDisplacementTask, setHasActiveDisplacementTask] = useState(false);
  const [viewerAccess, setViewerAccess] = useState<{ jobId: string; state: 'loading' | 'allowed' | 'denied'; job?: DisplacementJob } | null>(null);

  const navigateTo = useCallback((nextPath: string) => {
    const normalizedPath = navigatePath(nextPath, window.location.pathname, window.history.pushState.bind(window.history));
    pathnameRef.current = normalizedPath;
    setPathname(normalizedPath);
  }, []);

  useEffect(() => {
    const handlePopState = () => {
      const nextPath = normalizePathname(window.location.pathname);
      pathnameRef.current = nextPath;
      setPathname(nextPath);
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  const refreshDisplacementTaskState = useCallback(async (currentUser: User) => {
    try {
      const jobs = await listDisplacementJobs(currentUser);
      setHasActiveDisplacementTask(jobs.some((job) => ['queued', 'dispatching', 'running'].includes(job.state)));
    } catch {
      // A backend outage must not create a false navigation lock. The task
      // screen presents its own recoverable account-state error instead.
    }
  }, []);

  useEffect(() => {
    if (!user) {
      setHasActiveDisplacementTask(false);
      return undefined;
    }
    void refreshDisplacementTaskState(user);
    const timer = window.setInterval(() => { void refreshDisplacementTaskState(user); }, 5000);
    return () => window.clearInterval(timer);
  }, [refreshDisplacementTaskState, user]);

  useEffect(() => {
    const context = isLandslideVisualizationRoute(pathname)
      ? resolveLangtangRouteContext(window.location.search)
      : null;
    if (!user || !context || (context.mode === 'static-langtang' && isLangtangExplorerEnabled())) {
      setViewerAccess(null);
      return undefined;
    }
    let active = true;
    setViewerAccess({ jobId: context.jobId, state: 'loading' });
    void getDisplacementJob(context.jobId, user).then((job) => {
      if (!active) return;
      setViewerAccess({
        jobId: context.jobId,
        state: visualizationForJob(job).available ? 'allowed' : 'denied',
        job,
      });
    }).catch(() => {
      if (active) setViewerAccess({ jobId: context.jobId, state: 'denied' });
    });
    return () => { active = false; };
  }, [pathname, user]);

  useEffect(() => {
    if (!isLandslideLangtangRoute(pathname) || new URLSearchParams(window.location.search).get('mode') !== 'job') return;
    const canonicalUrl = `${LANDSLIDE_RESULT_PATH}${window.location.search}${window.location.hash}`;
    window.history.replaceState({}, '', canonicalUrl);
    pathnameRef.current = LANDSLIDE_RESULT_PATH;
    setPathname(LANDSLIDE_RESULT_PATH);
  }, [pathname]);

  useEffect(() => {
    // The account task may continue while the user visits Home. Only the map
    // workspace is locked because changing its AOI during an active task would
    // be misleading; the durable status route stays available.
    if (hasActiveDisplacementTask && pathname === LANDSLIDE_PATH) {
      navigateTo(LANDSLIDE_DISPLACEMENT_PATH);
    }
  }, [hasActiveDisplacementTask, navigateTo, pathname]);

  useEffect(() => {
    if (!auth) {
      setAuthState('idle');
      return undefined;
    }

    return onAuthStateChanged(auth, (currentUser) => {
      setUser(currentUser);
      setAuthState(currentUser ? 'ready' : 'idle');
      setAuthError(null);
      const currentPath = pathnameRef.current;
      if (!currentUser && isLandslideRoute(currentPath)) {
        setPendingRoute(currentPath);
        setAuthModalOpen(true);
        navigateTo('/');
      }
    }, (error) => {
      setAuthError(error.message);
      setAuthState('error');
    });
  }, [navigateTo]);

  const handleSignIn = async () => {
    if (!auth || !googleProvider) {
      setAuthError('Firebase is not configured for this local environment.');
      setAuthState('error');
      return;
    }

    setAuthState('loading');
    try {
      await signInWithPopup(auth, googleProvider);
      setAuthState('ready');
      setAuthError(null);
      setAuthModalOpen(false);
      if (pendingRoute) {
        navigateTo(pendingRoute);
        setPendingRoute(null);
      }
    } catch (error: unknown) {
      setAuthError(error instanceof Error ? error.message : 'Authentication failed');
      setAuthState('error');
    }
  };

  const handleSignOut = async () => {
    if (!auth) return;
    try {
      await signOut(auth);
      setPendingRoute(null);
      setAuthModalOpen(false);
      if (pathname === '/landslide' || pathname.startsWith('/landslide/')) {
        navigateTo('/');
      }
    } catch (error: unknown) {
      setAuthError(error instanceof Error ? error.message : 'Sign out failed');
    }
  };

  const handleSelectEngine = (engine: Engine) => {
    if (!user) {
      if (engine.name === 'Landslide') {
        setPendingRoute('/landslide');
      } else {
        setPendingRoute(null);
      }
      setAuthModalOpen(true);
      return;
    }

    if (engine.name === 'Landslide') {
      navigateTo('/landslide');
      return;
    }

    setAuthModalOpen(false);
  };

  if (isLandslideRoute(pathname)) {
    if (!user) {
      return (
        <Home
          user={user}
          authState={authState}
          authError={authError}
          firebaseConfigured={firebaseConfigured}
          missingFirebaseConfig={missingFirebaseConfig}
          showAuthModal={true}
          onCloseAuthModal={() => { setAuthModalOpen(false); setPendingRoute(null); }}
          onSignIn={handleSignIn}
          onSignOut={handleSignOut}
          onSelectEngine={handleSelectEngine}
        />
      );
    }

    if (isLandslideVisualizationRoute(pathname)) {
      const viewerContext = resolveLangtangRouteContext(window.location.search);
      const flagEnabled = isLangtangExplorerEnabled();
      const contextVerified = viewerContext !== null
        && viewerAccess?.jobId === viewerContext.jobId
        && viewerAccess.state === 'allowed';
      const requiresVerification = viewerContext?.mode === 'job' || (viewerContext !== null && !flagEnabled);
      if (requiresVerification && viewerContext && (!viewerAccess || viewerAccess.jobId !== viewerContext.jobId || viewerAccess.state === 'loading')) {
        return <main className="langtang-unavailable" role="status">Verifying processing result…</main>;
      }
      if (viewerContext?.mode === 'job') {
        if (!contextVerified || !viewerAccess?.job) {
          return <main className="langtang-unavailable" role="alert">
            <span>DISPLACEMENT RESULT EXPLORER</span>
            <h1>This task result cannot be visualized.</h1>
            <p>The task is unavailable, incomplete, or no longer has a verified derived-result package.</p>
            <button type="button" onClick={() => navigateTo(LANDSLIDE_DISPLACEMENT_PATH)}>Processing results</button>
          </main>;
        }
        return <Suspense fallback={<main className="langtang-unavailable" role="status">Loading displacement result explorer…</main>}>
          <JobResultExplorerPage
            job={viewerAccess.job}
            user={user}
            onHome={() => navigateTo('/')}
            onBack={() => navigateTo(viewerContext.returnPath)}
            onSignOut={handleSignOut}
          />
        </Suspense>;
      }
      const staticContext = viewerContext?.mode === 'static-langtang' ? viewerContext : null;
      return <Suspense fallback={<main className="langtang-unavailable" role="status">Loading Langtang explorer…</main>}>
        <LangtangExplorerPage
          enabled={canOpenLangtangExplorer(flagEnabled, contextVerified ? staticContext : null)}
          user={user}
          onHome={() => navigateTo('/')}
          onBack={() => navigateTo(staticContext?.returnPath ?? LANDSLIDE_PATH)}
          manifestPath={staticContext?.manifestPath}
          backLabel={staticContext ? 'Processing results' : 'Displacement map'}
          onSignOut={handleSignOut}
        />
      </Suspense>;
    }

    return (
      <LandslidePage
        key={user.uid}
        user={user}
        onHome={() => navigateTo('/')}
        isResultsPage={isLandslideResultsRoute(pathname)}
        isDisplacementPage={isLandslideDisplacementRoute(pathname)}
        onDisplacements={() => navigateTo(LANDSLIDE_DISPLACEMENT_PATH)}
        onLangtangExplorer={() => navigateTo(LANDSLIDE_LANGTANG_PATH)}
        showLangtangExplorer={isLangtangExplorerEnabled()}
        onResults={() => navigateTo(LANDSLIDE_RESULTS_PATH)}
        onBackToMap={() => navigateTo(LANDSLIDE_PATH)}
        onDisplacementTaskActivityChange={setHasActiveDisplacementTask}
        onSignOut={handleSignOut}
      />
    );
  }

  return (
    <Home
      user={user}
      authState={authState}
      authError={authError}
      firebaseConfigured={firebaseConfigured}
      missingFirebaseConfig={missingFirebaseConfig}
      showAuthModal={authModalOpen}
      onCloseAuthModal={() => { setAuthModalOpen(false); setPendingRoute(null); }}
      onSignIn={handleSignIn}
      onSignOut={handleSignOut}
      onSelectEngine={handleSelectEngine}
    />
  );
}

export default App;
