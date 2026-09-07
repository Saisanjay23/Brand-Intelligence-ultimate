import "@testing-library/jest-dom/vitest";

// jsdom here exposes `localStorage` as a bare `{}` -- no getItem, no
// setItem, no clear, nothing. That is quietly poisonous rather than loudly
// broken: every module that persists anything (savedClients.ts,
// clientKeywords.ts, scheduleRunner.ts) wraps its storage access in
// try/catch and degrades to "this just won't survive a reload", so a test
// written against persisted behaviour would exercise the degraded path
// instead and pass for the wrong reason.
//
// Installed only when the environment has not supplied a real one.
if (typeof (globalThis as { localStorage?: Partial<Storage> }).localStorage?.getItem !== "function") {
  const store = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return store.size;
    },
    key: (i: number) => [...store.keys()][i] ?? null,
    getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k: string, v: string) => {
      store.set(String(k), String(v));
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
    clear: () => {
      store.clear();
    },
  };
  const descriptor = { value: storage, configurable: true, writable: true };
  Object.defineProperty(globalThis, "localStorage", descriptor);
  if (typeof window !== "undefined") Object.defineProperty(window, "localStorage", descriptor);
}
