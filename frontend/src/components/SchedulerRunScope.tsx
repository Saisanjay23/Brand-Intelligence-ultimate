// What the Scheduler should run for one client: which platforms, and which
// keyword types.
//
// SAME INTERACTION AS THE CLIENTS PAGE, on purpose. Chips that toggle, and
// an EMPTY selection meaning "all" -- an analyst who has learned it on one
// screen already knows it here. It is also the same thing the API means:
// omitting `platforms` sweeps every ready platform, so "no chips lit" and
// "All" are one state rather than two that have to be kept in step.
//
// SAVED ON THE CLIENT, NOT ON THE QUEUE ENTRY. The queue is this browser's
// localStorage and gets cleared, reset and rebuilt; the choice of what to
// sweep is a property of the client and should outlive all of that. It goes
// to PUT /clients/{id}/scheduler-prefs, a narrow write that cannot disturb
// the client's keywords or caps -- and which saving the Clients form cannot
// reset, because that form does not send these fields.
import { useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import { clientsApi } from "../api/clientsApi";
import type { Client, PlatformState } from "../api/types";
import { refresh as refreshDirectory } from "../services/clientDirectory";
import { PlatformIcon } from "./PlatformIcon";

const KEYWORD_SCOPES: { id: string; label: string; hint: string }[] = [
  { id: "", label: "Both", hint: "Sweep individual names and domain keywords" },
  { id: "individual", label: "Individual", hint: "Executive / person names only" },
  { id: "domain", label: "Domain", hint: "Brand / product keywords only" },
];

interface Props {
  client: Client;
  platforms: PlatformState[];
  disabled?: boolean;
}

function chip(on: boolean, colour: string): React.CSSProperties {
  return {
    display: "inline-flex", alignItems: "center", gap: "5px",
    padding: "3px 9px", borderRadius: "14px", cursor: "pointer",
    fontSize: "10px", fontWeight: 700, lineHeight: 1.6,
    border: `1px solid ${on ? colour : "var(--border-color)"}`,
    background: on ? "var(--bg-surface)" : "transparent",
    color: on ? colour : "var(--text-dim)",
  };
}

export function SchedulerRunScope({ client, platforms, disabled }: Props) {
  const [busy, setBusy] = useState(false);

  // WHY A REF AND A DEBOUNCE, not just state.
  //
  // Two chips clicked in the same tick both run before React re-renders, so
  // both read the SAME `selected` and the second overwrites the first --
  // measured live, clicking Facebook then Twitter saved only Twitter and
  // silently dropped Facebook. State cannot fix that: `setState` is async by
  // design. A ref updates synchronously, so each click composes onto what
  // the previous one actually intended.
  //
  // The save is then DEBOUNCED rather than fired per click. Without it, a
  // burst of clicks fires a burst of PUTs whose responses can land out of
  // order, and an early partial one arriving last would overwrite the final
  // state on the server. One request carrying the settled selection has no
  // such ordering to get wrong.
  const savedPlatforms = client.scheduler_platforms || [];
  const savedScope = client.scheduler_keyword_scope || "";

  // What the analyst has asked for, ahead of the server. Null means "no
  // local intent -- show what the server has".
  const intended = useRef<{ platforms: string[]; scope: string } | null>(null);
  const [, forceRender] = useState(0);
  const timer = useRef<number | null>(null);

  // The server has caught up (or the client changed under us): drop the
  // local intent so the chips can never be left showing something unsaved.
  useEffect(() => {
    const i = intended.current;
    if (!i) return;
    const samePlatforms =
      i.platforms.length === savedPlatforms.length &&
      i.platforms.every((p) => savedPlatforms.includes(p));
    if (samePlatforms && i.scope === savedScope) {
      intended.current = null;
      forceRender((n) => n + 1);
    }
  }, [savedPlatforms, savedScope]);

  const current = () =>
    intended.current ?? { platforms: [...savedPlatforms], scope: savedScope };

  const selected = new Set(current().platforms);
  const scope = current().scope;

  /** Record the intent synchronously, then save once the clicking stops. */
  const apply = (next: { platforms?: string[]; scope?: string }) => {
    const now = current();
    intended.current = {
      platforms: next.platforms ?? now.platforms,
      scope: next.scope ?? now.scope,
    };
    forceRender((n) => n + 1);

    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      const send = intended.current;
      if (!send) return;
      setBusy(true);
      clientsApi
        .setSchedulerPrefs(client.client_id, {
          platforms: send.platforms,
          keyword_scope: send.scope,
        })
        // Re-read rather than trust the local copy: the runner reads this
        // client fresh when its turn comes up, so the screen and the sweep
        // must agree on one record.
        .then(() => refreshDirectory())
        .catch((e) => {
          // Roll back -- a chip left lit after a failed save is a lie about
          // what will actually be swept.
          intended.current = null;
          forceRender((n) => n + 1);
          toast.error((e as Error).message || "Could not save the run scope");
        })
        .finally(() => setBusy(false));
    }, 350);
  };

  // A pending save must not be lost because the row unmounted (the queue
  // re-rendered, the analyst switched tabs).
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  const togglePlatform = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    // Every platform selected IS "all" -- collapsing to empty keeps one
    // canonical spelling of that state, so the All chip lights instead of
    // every individual one.
    apply({ platforms: next.size === platforms.length ? [] : [...next] });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "6px", opacity: busy ? 0.6 : 1 }}>
      <div style={{ display: "flex", alignItems: "center", gap: "5px", flexWrap: "wrap" }}>
        <span style={{ fontSize: "9px", fontWeight: 700, letterSpacing: "0.6px",
                       textTransform: "uppercase", color: "var(--text-dim)", marginRight: 2 }}>
          Platforms
        </span>
        <button
          type="button"
          disabled={disabled}
          onClick={() => apply({ platforms: [] })}
          title="Sweep every platform that has a ready session"
          style={chip(selected.size === 0, "var(--accent, #7c5cff)")}
        >
          All
        </button>
        {platforms.map((p) => (
          <button
            key={p.platform}
            type="button"
            disabled={disabled}
            onClick={() => togglePlatform(p.platform)}
            title={`${selected.has(p.platform) ? "Remove" : "Add"} ${p.name}`
                   + (p.session_state === "ready" ? "" : ` — session ${p.session_state}`)}
            style={{ ...chip(selected.has(p.platform), "var(--cyan-bright, var(--cyan))"),
                     // A platform with no usable session is still selectable:
                     // it may be fixed before this client's turn arrives.
                     opacity: p.session_state === "ready" ? 1 : 0.55 }}
          >
            <PlatformIcon platform={p.platform} size={11} />
            {p.name}
          </button>
        ))}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: "5px", flexWrap: "wrap" }}>
        <span style={{ fontSize: "9px", fontWeight: 700, letterSpacing: "0.6px",
                       textTransform: "uppercase", color: "var(--text-dim)", marginRight: 2 }}>
          Keywords
        </span>
        {KEYWORD_SCOPES.map((k) => (
          <button
            key={k.id || "all"}
            type="button"
            disabled={disabled}
            onClick={() => apply({ scope: k.id })}
            title={k.hint}
            style={chip(scope === k.id, "var(--purple)")}
          >
            {k.label}
          </button>
        ))}
      </div>
    </div>
  );
}
