import { describe, expect, it, vi } from "vitest";
import type * as SessionsApiModule from "@/lib/sessionsApi";

const updateSessionMock = vi.fn();
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  updateSession: (...args: unknown[]) => updateSessionMock(...args),
}));

import { handleSessionEvent, useChatStore } from "@/store/chatStore";
import { parseEvent } from "@/lib/sse";

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
