import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The Turbo switch reports a failed PATCH through the shared toast helper.
const toastSpy = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toast", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  showToast: (...args: unknown[]) => toastSpy(...args),
}));
// Composer reads workspace files via a TanStack query hook (for "@"-file
// mentions); stub it so bare Composer renders need no QueryClientProvider.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});
// The Turbo switch PATCHes through the store's `updateSession`; spy on it so
// the tests assert the wire call (and drive failures) without a network.
const updateSessionMock = vi.fn(async (_id: string, updates: Record<string, unknown>) => ({
  id: _id,
  turbo: updates.turbo === true,
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  updateSession: (...args: Parameters<typeof updateSessionMock>) => updateSessionMock(...args),
}));
// ComposerStatusLine's PR link, HostBadge and the harness labels read through
// TanStack Query; stub them so bare renders need no QueryClient provider.
vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: { hostId: null }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: () => undefined,
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({ codex: "Codex" }),
}));

import { parseEvent } from "@/lib/sse";
import { bindConversationForTest, handleSessionEvent, useChatStore } from "@/store/chatStore";
import { Composer } from "./ChatPage";

const CODEX_MODEL_OPTIONS = [
  { id: "gpt-5.5", displayName: "GPT-5.5" },
  { id: "gpt-5.5-mini", displayName: "GPT-5.5 mini" },
];

/** A writable, idle Codex terminal-first composer (the Turbo surface). */
function codexComposer(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return (
    <Composer
      status="idle"
      isWorking={false}
      disabled={false}
      onSend={vi.fn()}
      onStop={vi.fn()}
      agents={undefined}
      selectedAgentId={null}
      permissionLevel={null}
      readOnlyReason={null}
      replyQuotes={[]}
      onRemoveQuote={vi.fn()}
      onClearAllQuotes={vi.fn()}
      effortLevels={["low", "medium", "high"]}
      showEffort
      showCodexPlanMode={false}
      isTerminalFirst
      isNativeWrapper
      showModels
      modelPickerKind="codex"
      codexModelOptions={CODEX_MODEL_OPTIONS}
      {...overrides}
    />
  );
}

/** Open the composer gear's config modal and flip its Turbo switch. */
async function clickTurboSwitch() {
  fireEvent.click(screen.getByTestId("composer-config-gear"));
  fireEvent.click(await screen.findByTestId("composer-config-turbo"));
}

describe("Composer Turbo chip and switch feedback", () => {
  beforeEach(() => {
    updateSessionMock.mockClear();
    toastSpy.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("the chip follows the optimistic flip and disappears on rollback", async () => {
    bindConversationForTest("conv_turbo_opt", { turbo: false });
    let reject!: (e: unknown) => void;
    updateSessionMock.mockImplementationOnce(
      () =>
        new Promise((_res, rej) => {
          reject = rej;
        }),
    );
    render(codexComposer());
    expect(screen.queryByTestId("composer-turbo-chip")).toBeNull();
    await clickTurboSwitch();
    // Optimistic: the chip is up before the PATCH answers.
    expect(screen.getByTestId("composer-turbo-chip").textContent).toMatch(/paid credits/i);
    await act(async () => {
      reject(new Error("403 forbidden"));
    });
    await waitFor(() => expect(screen.queryByTestId("composer-turbo-chip")).toBeNull());
    expect(screen.getByTestId("composer-config-turbo-state").textContent).toBe("Off");
  });

  it("a failed Turbo OFF rolls back to ON, brings the chip back and says Turbo is still On", async () => {
    bindConversationForTest("conv_turbo_off_fail", { turbo: true });
    updateSessionMock.mockImplementationOnce(async () => {
      throw new Error("500");
    });
    render(codexComposer());
    expect(screen.getByTestId("composer-turbo-chip")).toBeTruthy();
    await clickTurboSwitch();
    await waitFor(() =>
      expect(updateSessionMock).toHaveBeenCalledWith("conv_turbo_off_fail", { turbo: false }),
    );
    await waitFor(() => expect(screen.getByTestId("composer-turbo-chip")).toBeTruthy());
    expect(useChatStore.getState().turbo).toBe(true);
    // R2-2: the rollback alone is easy to miss; Turbo decides paid-credit spend.
    await waitFor(() => expect(toastSpy).toHaveBeenCalledTimes(1));
    expect(String(toastSpy.mock.calls[0][0])).toMatch(/Couldn't change Turbo.*still On/);
  });

  it("a failed Turbo ON says Turbo is still Off", async () => {
    bindConversationForTest("conv_turbo_on_fail", { turbo: false });
    updateSessionMock.mockImplementationOnce(async () => {
      throw new Error("403 forbidden");
    });
    render(codexComposer());
    await clickTurboSwitch();
    await waitFor(() => expect(toastSpy).toHaveBeenCalledTimes(1));
    expect(String(toastSpy.mock.calls[0][0])).toMatch(/Couldn't change Turbo.*still Off/);
    expect(useChatStore.getState().turbo).toBe(false);
    expect(screen.queryByTestId("composer-turbo-chip")).toBeNull();
  });

  it("a successful toggle raises no toast", async () => {
    bindConversationForTest("conv_turbo_ok", { turbo: false });
    render(codexComposer());
    await clickTurboSwitch();
    await waitFor(() => expect(screen.getByTestId("composer-turbo-chip")).toBeTruthy());
    await waitFor(() =>
      expect(updateSessionMock).toHaveBeenCalledWith("conv_turbo_ok", { turbo: true }),
    );
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it("the chip follows server-pushed session.turbo events (another surface toggled)", async () => {
    bindConversationForTest("conv_turbo_sse", { turbo: false });
    render(codexComposer());
    expect(screen.queryByTestId("composer-turbo-chip")).toBeNull();
    const on = parseEvent("session.turbo", { conversation_id: "conv_turbo_sse", turbo: true });
    await act(async () => {
      handleSessionEvent(on!, "conv_turbo_sse");
    });
    expect(screen.getByTestId("composer-turbo-chip")).toBeTruthy();
    const off = parseEvent("session.turbo", { conversation_id: "conv_turbo_sse", turbo: false });
    await act(async () => {
      handleSessionEvent(off!, "conv_turbo_sse");
    });
    expect(screen.queryByTestId("composer-turbo-chip")).toBeNull();
  });

  it("the chip stays visible while a turn is running", () => {
    bindConversationForTest("conv_turbo_turn", { turbo: true });
    render(codexComposer({ status: "streaming", isWorking: true }));
    expect(screen.getByTestId("composer-turbo-chip")).toBeTruthy();
  });

  it("a read-only viewer still sees the chip", () => {
    bindConversationForTest("conv_turbo_ro", { turbo: true });
    render(codexComposer({ permissionLevel: 1 }));
    expect(screen.getByTestId("composer-turbo-chip")).toBeTruthy();
  });

  it("the chip names Turbo in its text and paid credits in its title", () => {
    bindConversationForTest("conv_turbo_a11y", { turbo: true });
    render(codexComposer());
    const chip = screen.getByTestId("composer-turbo-chip");
    expect(chip.textContent).toMatch(/Turbo/);
    expect(chip.getAttribute("title") ?? "").toMatch(/paid credits/);
  });

  it("the chip hides when the session leaves the Codex picker (agent switch)", () => {
    bindConversationForTest("conv_turbo_switch", { turbo: true });
    const view = render(codexComposer());
    expect(screen.getByTestId("composer-turbo-chip")).toBeTruthy();
    view.rerender(codexComposer({ modelPickerKind: "claude" }));
    expect(screen.queryByTestId("composer-turbo-chip")).toBeNull();
    view.rerender(codexComposer({ modelPickerKind: null, showModels: false }));
    expect(screen.queryByTestId("composer-turbo-chip")).toBeNull();
  });
});
