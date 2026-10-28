import { useEffect, useRef, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";

interface Continuation {
  revision?: number;
  guarded?: boolean;
  status?: string;
  wait_id?: string;
  automatic?: {
    status?: string;
    wait_id?: string;
    remaining_attempts?: number;
    remaining_booked_ppm?: number;
    expires_at_epoch?: number;
    next_retry_epoch?: number;
  };
}

const WAIT_ID = /^[a-f0-9]{32}$/i;
const HOLD = /(?:^|_)hold$/;
const CANCELABLE = new Set(["activated", "waiting", "held"]);

function label(status: string | undefined) {
  if (!status) return "Status unavailable";
  if (HOLD.test(status)) return "Owner review required";
  const labels: Record<string, string> = {
    unavailable: "Unavailable",
    disabled: "Unavailable",
    awaiting_authorization: "Ready for owner authorization",
    activated: "Active",
    waiting: "Waiting for the next retry window",
    held: "Paused",
    completed: "Completed",
    cancelled: "Cancelled",
    cap_exhausted: "Attempt limit reached",
    expired: "Authorization expired",
  };
  return labels[status] ?? "Status unavailable";
}

export function OwnerContinuationPanel({
  sessionId,
  bindingId,
  active,
}: {
  sessionId: string;
  bindingId: string;
  active: boolean;
}) {
  const [state, setState] = useState<Continuation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [queuedStart, setQueuedStart] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [queuedCancelStatus, setQueuedCancelStatus] = useState<string | null>(null);
  const inFlight = useRef(false);
  const base = `/v1/sessions/${encodeURIComponent(sessionId)}/docloop/owner-continuation`;

  useEffect(() => {
    if (!active) return;
    let stopped = false;
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const response = await authenticatedFetch(base, { signal: controller.signal });
        if (!response.ok) throw new Error();
        const next = (await response.json()) as Continuation;
        if (!stopped) {
          setState(next);
          setError(null);
          if (next.automatic?.status !== "awaiting_authorization") setQueuedStart(false);
          if (queuedCancelStatus && next.automatic?.status !== queuedCancelStatus)
            setQueuedCancelStatus(null);
        }
      } catch {
        if (!stopped && !controller.signal.aborted)
          setError("Automatic recovery status could not be loaded.");
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => {
      stopped = true;
      controller.abort();
      clearInterval(timer);
    };
  }, [active, base, queuedCancelStatus]);

  const automatic = state?.automatic;
  const waitId = state?.wait_id ?? automatic?.wait_id;
  const canStart =
    Boolean(active) &&
    state?.guarded === true &&
    state.status === "waiting" &&
    typeof state.revision === "number" &&
    typeof waitId === "string" &&
    WAIT_ID.test(waitId) &&
    automatic?.status === "awaiting_authorization" &&
    !queuedStart;
  const canCancel = Boolean(
    active &&
    bindingId &&
    automatic?.status &&
    !queuedCancelStatus &&
    (queuedStart || CANCELABLE.has(automatic.status)),
  );

  async function queue(action: "automatic" | "cancel") {
    if (inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      const response = await authenticatedFetch(`${base}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-docloop-edit": "1" },
        body: JSON.stringify(
          action === "automatic"
            ? { binding_id: bindingId, revision: state?.revision, wait_id: waitId }
            : { binding_id: bindingId },
        ),
      });
      if (response.status !== 202) throw new Error();
      const result = (await response.json()) as { status?: string };
      if (result.status !== "queued") throw new Error();
      if (action === "automatic") setQueuedStart(true);
      else setQueuedCancelStatus(automatic?.status ?? null);
      setNotice(
        action === "automatic"
          ? "Request queued. The supervisor must authorize it before retries begin."
          : "Stop request queued. A retry already in progress may finish.",
      );
    } catch {
      setError("The request could not be queued. Refresh the status before trying again.");
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  const status = automatic?.status;
  const configured = Boolean(status && status !== "disabled" && status !== "unavailable");
  const hold = Boolean(status && HOLD.test(status));
  return (
    <section
      className="docloop-notebook-message docloop-owner-continuation"
      aria-label="Automatic recovery"
    >
      <strong>Automatic recovery</strong>
      <p role="status">{configured ? label(status) : "Unavailable"}</p>
      {typeof automatic?.remaining_attempts === "number" && (
        <p>{automatic.remaining_attempts} retries remaining</p>
      )}
      {typeof automatic?.remaining_booked_ppm === "number" && (
        <p>Booked budget remaining: {automatic.remaining_booked_ppm} ppm</p>
      )}
      {typeof automatic?.next_retry_epoch === "number" && automatic.next_retry_epoch > 0 && (
        <p>
          Next retry window:{" "}
          <time dateTime={new Date(automatic.next_retry_epoch * 1000).toISOString()}>
            {new Date(automatic.next_retry_epoch * 1000).toLocaleString()}
          </time>
        </p>
      )}
      {typeof automatic?.expires_at_epoch === "number" && automatic.expires_at_epoch > 0 && (
        <p>
          Authorization expires:{" "}
          <time dateTime={new Date(automatic.expires_at_epoch * 1000).toISOString()}>
            {new Date(automatic.expires_at_epoch * 1000).toLocaleString()}
          </time>
        </p>
      )}
      {hold && <p>Owner review is required before this can continue.</p>}
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
      {canStart && (
        <button type="button" disabled={submitting} onClick={() => void queue("automatic")}>
          Queue automatic recovery
        </button>
      )}
      {canCancel && (
        <button type="button" disabled={submitting} onClick={() => void queue("cancel")}>
          Queue stop for future retries
        </button>
      )}
    </section>
  );
}
