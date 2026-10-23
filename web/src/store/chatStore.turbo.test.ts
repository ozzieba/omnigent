import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as SessionsApiModule from "@/lib/sessionsApi";

const updateSessionMock = vi.fn();
const getSessionSlimMock = vi.fn();
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  updateSession: (...args: unknown[]) => updateSessionMock(...args),
  getSessionSlim: (...args: unknown[]) => getSessionSlimMock(...args),
}));

import { bindConversationForTest, handleSessionEvent, useChatStore } from "@/store/chatStore";
import { parseEvent } from "@/lib/sse";

interface TurboEcho {
  id: string;
  turbo: boolean;
  costControlModeOverride?: string | null;
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Queue one PATCH response per call, in call order; settle them in any order. */
function queuePatches(count: number) {
  const calls = Array.from({ length: count }, () => deferred<TurboEcho>());
  for (const call of calls) updateSessionMock.mockImplementationOnce(() => call.promise);
  return calls;
}

describe("Turbo store + SSE", () => {
  it("optimistic flip then rollback on a failed PATCH", async () => {
    useChatStore.setState({ conversationId: "conv_t", turbo: false });
    let reject!: (e: unknown) => void;
    updateSessionMock.mockImplementationOnce(
      () =>
        new Promise((_res, rej) => {
          reject = rej;
        }),
    );
    const p = useChatStore.getState().setTurbo(true);
    expect(useChatStore.getState().turbo).toBe(true);
    reject(new Error("403 forbidden"));
    await expect(p).rejects.toThrow("403");
    expect(useChatStore.getState().turbo).toBe(false);
    expect(updateSessionMock).toHaveBeenCalledWith("conv_t", { turbo: true });
  });

  it("success keeps the server echo", async () => {
    useChatStore.setState({ conversationId: "conv_t", turbo: false });
    updateSessionMock.mockResolvedValueOnce({ id: "conv_t", turbo: true });
    await useChatStore.getState().setTurbo(true);
    expect(useChatStore.getState().turbo).toBe(true);
  });

  it("parses session.turbo and mirrors it into the active conversation", () => {
    const evt = parseEvent("session.turbo", {
      type: "session.turbo",
      conversation_id: "conv_t",
      turbo: true,
      sequence_number: null,
    });
    expect(evt).toEqual({ type: "session_turbo", conversationId: "conv_t", turbo: true });
    useChatStore.setState({ conversationId: "conv_t", turbo: false });
    handleSessionEvent(evt!, "conv_t");
    expect(useChatStore.getState().turbo).toBe(true);
    expect(parseEvent("session.turbo", { conversation_id: "conv_t", turbo: "yes" })).toBeNull();
  });
});

// R2-4: two quick toggles can be answered out of order. Only the newest
// request may settle the switch; an older echo (or rollback) must not
// resurrect a value the user has already replaced.
describe("Turbo write ordering (R2-4)", () => {
  beforeEach(() => {
    updateSessionMock.mockReset();
    getSessionSlimMock.mockReset();
    // A failed newest write reads Turbo back; by default that read fails, which
    // keeps the rollback, so tests that do not care about it stay deterministic.
    getSessionSlimMock.mockRejectedValue(new Error("not mocked"));
  });

  it("in-order responses: on then off ends off", async () => {
    bindConversationForTest("conv_seq_inorder", { turbo: false });
    const [a, b] = queuePatches(2);
    const pa = useChatStore.getState().setTurbo(true);
    const pb = useChatStore.getState().setTurbo(false);
    a.resolve({ id: "conv_seq_inorder", turbo: true });
    await pa;
    b.resolve({ id: "conv_seq_inorder", turbo: false });
    await pb;
    expect(useChatStore.getState().turbo).toBe(false);
  });

  it("a late echo of a superseded ON does not turn Turbo back on", async () => {
    bindConversationForTest("conv_seq_late_on", { turbo: false });
    const [a, b] = queuePatches(2);
    const pa = useChatStore.getState().setTurbo(true);
    const pb = useChatStore.getState().setTurbo(false);
    b.resolve({ id: "conv_seq_late_on", turbo: false });
    await pb;
    a.resolve({ id: "conv_seq_late_on", turbo: true });
    await pa;
    expect(useChatStore.getState().turbo).toBe(false);
  });

  it("a late echo of a superseded OFF does not hide a newer ON", async () => {
    bindConversationForTest("conv_seq_late_off", { turbo: true });
    const [a, b] = queuePatches(2);
    const pa = useChatStore.getState().setTurbo(false);
    const pb = useChatStore.getState().setTurbo(true);
    b.resolve({ id: "conv_seq_late_off", turbo: true });
    await pb;
    a.resolve({ id: "conv_seq_late_off", turbo: false });
    await pa;
    expect(useChatStore.getState().turbo).toBe(true);
  });

  it("a superseded request's failure neither rolls back the newer toggle nor throws", async () => {
    bindConversationForTest("conv_seq_stale_fail", { turbo: false });
    const [a, b, c] = queuePatches(3);
    const pa = useChatStore.getState().setTurbo(true);
    const pb = useChatStore.getState().setTurbo(false);
    const pc = useChatStore.getState().setTurbo(true);
    c.resolve({ id: "conv_seq_stale_fail", turbo: true });
    await pc;
    b.resolve({ id: "conv_seq_stale_fail", turbo: false });
    await pb;
    a.reject(new Error("500"));
    const outcome = await pa.then(
      () => "resolved",
      () => "rejected",
    );
    expect(useChatStore.getState().turbo).toBe(true);
    expect(outcome).toBe("resolved");
  });

  it("a failed newest toggle rolls back to the last confirmed value, not an older optimistic one", async () => {
    // On; the user flips Off (A) then On (B) and both PATCHes fail. The server
    // still holds On, so the switch must read On. Rolling back to B's
    // "previous" (A's never-confirmed Off) would claim paid credits are off
    // while the session may still spend them.
    bindConversationForTest("conv_seq_double_fail", { turbo: true });
    const [a, b] = queuePatches(2);
    const pa = useChatStore.getState().setTurbo(false);
    const pb = useChatStore.getState().setTurbo(true);
    a.reject(new Error("500"));
    await pa.catch(() => undefined);
    b.reject(new Error("500"));
    await expect(pb).rejects.toThrow("500");
    expect(useChatStore.getState().turbo).toBe(true);
  });

  it("after the newest write fails, the switch is read back from the server (older write applied)", async () => {
    // Off; the user flips On (A, slow) then Off (B). B fails and rolls back to Off;
    // A then lands and the server holds On. Only the server can say which write
    // stuck, so the switch is read back once nothing is in flight.
    bindConversationForTest("conv_seq_newest_fail_old_ok", { turbo: false });
    const [a, b] = queuePatches(2);
    getSessionSlimMock.mockResolvedValueOnce({ id: "conv_seq_newest_fail_old_ok", turbo: true });
    const pa = useChatStore.getState().setTurbo(true);
    const pb = useChatStore.getState().setTurbo(false);
    b.reject(new Error("500"));
    await expect(pb).rejects.toThrow("500");
    expect(useChatStore.getState().turbo).toBe(false);
    expect(getSessionSlimMock).not.toHaveBeenCalled(); // A still in flight
    a.resolve({ id: "conv_seq_newest_fail_old_ok", turbo: true });
    await pa;
    await vi.waitFor(() => expect(useChatStore.getState().turbo).toBe(true));
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
  });

  it("after the newest write fails, the switch is read back from the server (failed reply was applied)", async () => {
    // On; A = Off (slow), B = On. The server applies A then B and holds On, but
    // B's reply is lost (504). A's late echo (Off) must not decide the switch.
    bindConversationForTest("conv_seq_ambiguous_fail", { turbo: true });
    const [a, b] = queuePatches(2);
    getSessionSlimMock.mockResolvedValueOnce({ id: "conv_seq_ambiguous_fail", turbo: true });
    const pa = useChatStore.getState().setTurbo(false);
    const pb = useChatStore.getState().setTurbo(true);
    b.reject(new Error("504"));
    await expect(pb).rejects.toThrow("504");
    a.resolve({ id: "conv_seq_ambiguous_fail", turbo: false });
    await pa;
    await vi.waitFor(() => expect(getSessionSlimMock).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(useChatStore.getState().turbo).toBe(true));
  });

  it("the routing PATCH echo does not override a Turbo toggle made while it was in flight", async () => {
    bindConversationForTest("conv_seq_routing", { turbo: false });
    const [routing, turbo] = queuePatches(2);
    const pr = useChatStore.getState().setCostControlMode("on");
    const pt = useChatStore.getState().setTurbo(true);
    turbo.resolve({ id: "conv_seq_routing", turbo: true });
    await pt;
    // The routing PATCH was answered before the Turbo write reached the server.
    routing.resolve({ id: "conv_seq_routing", turbo: false, costControlModeOverride: "on" });
    await pr;
    expect(useChatStore.getState().turbo).toBe(true);
    expect(useChatStore.getState().costControlModeOverride).toBe("on");
  });

  it("the routing PATCH echo still carries Turbo when no Turbo write overlapped it", async () => {
    bindConversationForTest("conv_seq_routing_quiet", { turbo: false });
    const [routing] = queuePatches(1);
    const pr = useChatStore.getState().setCostControlMode("on");
    routing.resolve({ id: "conv_seq_routing_quiet", turbo: true, costControlModeOverride: "on" });
    await pr;
    expect(useChatStore.getState().turbo).toBe(true);
  });

  it("an override refresh does not override a Turbo toggle made while it was in flight", async () => {
    bindConversationForTest("conv_seq_refresh", { turbo: false });
    const snapshot = deferred<TurboEcho>();
    getSessionSlimMock.mockImplementationOnce(() => snapshot.promise);
    const [turbo] = queuePatches(1);
    const pr = useChatStore.getState().refreshSessionOverrides();
    const pt = useChatStore.getState().setTurbo(true);
    turbo.resolve({ id: "conv_seq_refresh", turbo: true });
    await pt;
    snapshot.resolve({ id: "conv_seq_refresh", turbo: false });
    await pr;
    expect(useChatStore.getState().turbo).toBe(true);
  });
});
