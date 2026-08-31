import { useCallback, useEffect, useState } from "react";
import { Toaster } from "react-hot-toast";
import { clientsApi } from "./api/clientsApi";
import type { Client } from "./api/types";
import type { ViewPage } from "./components/Header";
import { AppLayout } from "./layouts/AppLayout";
import { AdminPanel } from "./pages/AdminPanel";
import { HomeView } from "./pages/HomeView";
import { LiveResultsView } from "./pages/LiveResultsView";
import { useDiscoveryJobPoll } from "./hooks/useDiscoveryJobPoll";
import { usePlatformState } from "./hooks/usePlatformState";
import { loadRecentClients, rememberClient, forgetClient, type RecentClient } from "./services/recentClients";

export default function App() {
  const [page, setPage] = useState<ViewPage>("home");
  const [theme, setTheme] = useState<"light" | "dark">(
    () => (localStorage.getItem("theme") as "light" | "dark") || "dark"
  );
  const [recentClients, setRecentClients] = useState<RecentClient[]>([]);
  const [allClients, setAllClients] = useState<Client[]>([]);
  const [clientId, setClientId] = useState("");
  const [clientName, setClientName] = useState("");
  const [error, setError] = useState("");
  // Bumped whenever a discovery job finishes, to force Live Results to
  // reload its cards.
  const [refreshKey, setRefreshKey] = useState(0);
  // Set when "Analyse Validated Profiles" (or Home's "Analyse" action)
  // starts a job -- handed to LiveResultsView, which switches its own
  // Discovery/Analysis toggle to Analysis and passes it into the embedded
  // AnalysisView so the analyst lands on the job already being watched
  // instead of an empty paste box. There is no standalone Analysis page
  // any more (removed -- Live Results' own toggle was a redundant second
  // entry point to the same tool).
  const [resumeAnalysisJobId, setResumeAnalysisJobId] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);

  // Client CRUD (create/edit/delete/list) has no backend behind it any
  // more -- the rebuilt backend only exposes discovery/analysis/sessions,
  // no /clients. Kept anyway (as the user asked) as the old UI's per-brand
  // scoping mental model: it renders, and the recent-clients list still
  // works (that's local/localStorage, see recentClients.ts), but the
  // "Saved Clients" list from the server will come back empty and any
  // save/delete will fail with a clear error toast rather than crash --
  // that's the accepted tradeoff of restoring the old frontend without
  // also restoring the old client_routes.py/controllers/dto/engine layer.
  const refreshAllClients = useCallback(() => {
    clientsApi.listClients().then((res) => setAllClients(res.items)).catch(() => {});
  }, []);

  useEffect(() => {
    refreshAllClients();
  }, [refreshAllClients]);

  useEffect(() => {
    const recents = loadRecentClients();
    setRecentClients(recents);
    if (recents.length) {
      setClientId(recents[0].client_id);
      setClientName(recents[0].name);
    }
  }, []);

  // Platform readiness and session pools, kept live by one poller and
  // refreshed the instant any panel changes a session, see the hook for
  // why these two have to move together.
  const {
    platforms,
    sessions,
    refresh: refreshPlatformState,
    error: platformStateError,
  } = usePlatformState();

  const onClient = useCallback(
    (id: string, name: string) => {
      setClientId(id);
      setClientName(name);
      if (id) setRecentClients(rememberClient({ client_id: id, name }));
      refreshAllClients();
    },
    [refreshAllClients],
  );

  const onForgetClient = useCallback(
    (id: string) => {
      const nextRecents = forgetClient(id);
      setRecentClients(nextRecents);
      setAllClients((prev) => {
        const remaining = prev.filter((c) => c.client_id !== id);
        if (clientId === id) {
          const nextClient = remaining[0] || nextRecents[0];
          setClientId(nextClient?.client_id || "");
          setClientName(nextClient?.name || "");
        }
        return remaining;
      });
      refreshAllClients();
    },
    [clientId, refreshAllClients],
  );

  // A finished sweep is the other moment session state changes on its
  // own -- a job that hit a login wall has just quarantined the session
  // it was holding. GET /discovery/jobs/{id} is a plain snapshot poll
  // (no event log, that route group is gone), see the hook.
  const discoveryPoll = useDiscoveryJobPoll(() => {
    refreshPlatformState();
    setRefreshKey((k) => k + 1);
  });

  return (
    <AppLayout
      page={page}
      onPage={setPage}
      clientId={clientId}
      clientName={clientName}
      recentClients={recentClients}
      allClients={allClients}
      onClient={onClient}
      onForgetClient={onForgetClient}
      activeJobsCount={discoveryPoll.running ? 1 : 0}
      readySessionsCount={sessions.filter((s) => s.state === "ready").length}
      platformCount={platforms.length}
      error={error || platformStateError}
      theme={theme}
      onThemeToggle={() => setTheme((prev) => (prev === "dark" ? "light" : "dark"))}
    >
      {page === "home" && (
        <HomeView
          clientId={clientId}
          clientName={clientName}
          platforms={platforms}
          onClient={onClient}
          onForgetClient={onForgetClient}
          busy={discoveryPoll.running}
          onStopDiscovery={discoveryPoll.cancel}
          stoppingDiscovery={discoveryPoll.cancelling}
          onDiscoveryStarted={(jobId) => {
            setError("");
            discoveryPoll.watch(jobId);
            setPage("results");
          }}
          onAnalyseStarted={(jobId) => {
            setError("");
            setResumeAnalysisJobId(jobId);
            setPage("results");
          }}
          onError={setError}
        />
      )}

      {page === "results" && (
        <LiveResultsView
          clientId={clientId}
          clientName={clientName}
          platforms={platforms}
          job={discoveryPoll.job}
          running={discoveryPoll.running}
          cancelling={discoveryPoll.cancelling}
          onCancel={discoveryPoll.cancel}
          refreshKey={refreshKey}
          resumeAnalysisJobId={resumeAnalysisJobId}
          onAnalyseStarted={(jobId) => {
            setError("");
            setResumeAnalysisJobId(jobId);
          }}
        />
      )}

      {page === "admin" && <AdminPanel sessions={sessions} onChanged={refreshPlatformState} />}
      <Toaster
        position="bottom-right"
        toastOptions={{
          style: {
            background: "rgba(16, 24, 40, 0.95)",
            color: "#fff",
            backdropFilter: "blur(10px)",
            border: "1px solid var(--border-subtle)",
            fontSize: "13px",
            fontFamily: "var(--font-main)",
          },
        }}
      />
    </AppLayout>
  );
}
