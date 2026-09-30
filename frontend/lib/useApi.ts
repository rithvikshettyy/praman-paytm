"use client";

import { useCallback, useEffect, useState } from "react";

import { api, errorText } from "@/lib/api";

interface State<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  updatedAt: Date | null;
}

/** GET a backend path, optionally every `refreshMs`. A failed refresh keeps the last data
 *  but reports the error, so a stale screen is never passed off as live. */
export function useApi<T>(path: string | null, refreshMs?: number) {
  const [state, setState] = useState<State<T>>({ data: null, error: null, loading: true, updatedAt: null });
  const [tick, setTick] = useState(0);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    api<T>(path)
      .then((data) => {
        if (!cancelled) setState({ data, error: null, loading: false, updatedAt: new Date() });
      })
      .catch((error) => {
        if (!cancelled) setState((previous) => ({ ...previous, error: errorText(error), loading: false }));
      });
    return () => {
      cancelled = true;
    };
  }, [path, tick]);

  useEffect(() => {
    if (!path || !refreshMs) return;
    const timer = window.setInterval(reload, refreshMs);
    return () => window.clearInterval(timer);
  }, [path, refreshMs, reload]);

  return { ...state, reload, setData: (data: T) => setState({ data, error: null, loading: false, updatedAt: new Date() }) };
}
