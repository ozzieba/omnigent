import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { parseEvent } from "@/lib/sse";
import { bindConversationForTest, handleSessionEvent, useChatStore } from "@/store/chatStore";
import { ChatHeader } from "./ChatHeader";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";

const toastSpy = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toast", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  showToast: (...args: unknown[]) => toastSpy(...args),
}));
const updateSessionMock = vi.fn(async (id: string, updates: Record<string, unknown>) => ({
  id,
  turbo: updates.turbo === true,
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  updateSession: (...args: Parameters<typeof updateSessionMock>) => updateSessionMock(...args),
}));

const mobileMenu = {
  fileViewerOpen: false,
  panelOpen: false,
  terminalFirst: true,
  executionLogsOpen: false,
  filesPanelOpen: false,
  subagentsPanelOpen: false,
  shellsPanelOpen: false,
  hideTerminalsTab: false,
  showShellsTab: false,
  terminalsLength: 0,
  debugMode: false,
  changedCount: 0,
  subagentsWorking: 0,
  agentCount: 1,
  onOpenFiles: () => {},
  onOpenChanges: () => {},
  onOpenShells: () => {},
  onOpenSubagents: () => {},
  onOpenMainExecutionLog: () => {},
};

function terminalFirstCtx(view: "chat" | "terminal"): TerminalFirstContextValue {
  return {
    isClaudeNative: false,
    isNativeWrapper: true,
    isTerminalFirst: true,
    isShellView: false,
    view,
    terminalViewKey: null,
    setView: () => {},
    terminalsAvailable: true,
    terminalStartingUp: false,
  };
}

/**
 * Header of a terminal-first session. ``turboControl`` is what AppShell passes
 * for a Codex session (``null`` for every other harness).
 */
function renderHeader({
  view,
  turboControl,
}: {
  view: "chat" | "terminal";
  turboControl: { canToggle: boolean } | null;
}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/c/sess-turbo"]}>
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <TerminalFirstContextProvider value={terminalFirstCtx(view)}>
            <ChatHeader
              sidebarOpen
              onOpenSidebar={() => {}}
              isChildSession={false}
              conversationId="sess-turbo"
              conversationTitle={null}
              projectName={null}
              boundAgent={undefined}
              wrapperLabel="codex-native-ui"
              canShare={false}
              onShare={() => {}}
              hasAgentInfo={false}
              onAgentInfo={() => {}}
              hasHeaderMenu={false}
              showFilesPanel={false}
              hasRailContent={false}
              rightPanelOpen={false}
              onToggleRightPanel={() => {}}
              mobileMenu={mobileMenu}
              turboControl={turboControl}
            />
          </TerminalFirstContextProvider>
        </TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

// R2-1: the composer (and its Turbo chip) unmounts in a terminal-first
// session's Terminal view, so the header must carry the paid-credits state.
describe("ChatHeader — Turbo chip in Terminal view", () => {
  beforeEach(() => {
    updateSessionMock.mockClear();
    toastSpy.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows the paid-credits chip in Terminal view for a Codex session with Turbo on", () => {
    bindConversationForTest("sess-turbo", { turbo: true });
    renderHeader({ view: "terminal", turboControl: { canToggle: true } });
    const chip = screen.getByTestId("header-turbo-chip");
    expect(chip.textContent).toMatch(/Turbo/);
    expect(chip.textContent).toMatch(/paid credits/);
    expect(
      screen.getByRole("button", { name: /Turbo is on: this session may spend paid credits/ }),
    ).toBeTruthy();
  });

  it("leaves the chip to the composer in Chat view", () => {
    bindConversationForTest("sess-turbo", { turbo: true });
    renderHeader({ view: "chat", turboControl: { canToggle: true } });
    expect(screen.queryByTestId("header-turbo-chip")).toBeNull();
  });

  it("shows no chip while Turbo is off", () => {
    bindConversationForTest("sess-turbo", { turbo: false });
    renderHeader({ view: "terminal", turboControl: { canToggle: true } });
    expect(screen.queryByTestId("header-turbo-chip")).toBeNull();
  });

  it("shows no chip for a non-Codex session even if the flag is set", () => {
    bindConversationForTest("sess-turbo", { turbo: true });
    renderHeader({ view: "terminal", turboControl: null });
    expect(screen.queryByTestId("header-turbo-chip")).toBeNull();
  });

  it("follows a server-pushed change while the terminal is shown", () => {
    bindConversationForTest("sess-turbo", { turbo: false });
    renderHeader({ view: "terminal", turboControl: { canToggle: true } });
    expect(screen.queryByTestId("header-turbo-chip")).toBeNull();
    const on = parseEvent("session.turbo", { conversation_id: "sess-turbo", turbo: true });
    act(() => {
      handleSessionEvent(on!, "sess-turbo");
    });
    expect(screen.getByTestId("header-turbo-chip")).toBeTruthy();
  });

  it("turns Turbo off from Terminal view through the chip's popover", async () => {
    bindConversationForTest("sess-turbo", { turbo: true });
    renderHeader({ view: "terminal", turboControl: { canToggle: true } });
    fireEvent.click(screen.getByTestId("header-turbo-chip-trigger"));
    const toggle = await screen.findByTestId("header-turbo-switch");
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(updateSessionMock).toHaveBeenCalledWith("sess-turbo", { turbo: false }),
    );
    await waitFor(() => expect(useChatStore.getState().turbo).toBe(false));
    expect(screen.queryByTestId("header-turbo-chip")).toBeNull();
  });

  it("a failed Turbo OFF from Terminal view keeps the chip and says Turbo is still On", async () => {
    bindConversationForTest("sess-turbo", { turbo: true });
    updateSessionMock.mockImplementationOnce(async () => {
      throw new Error("500");
    });
    renderHeader({ view: "terminal", turboControl: { canToggle: true } });
    fireEvent.click(screen.getByTestId("header-turbo-chip-trigger"));
    fireEvent.click(await screen.findByTestId("header-turbo-switch"));
    await waitFor(() => expect(toastSpy).toHaveBeenCalledTimes(1));
    expect(String(toastSpy.mock.calls[0][0])).toMatch(/Couldn't change Turbo.*still On/);
    expect(useChatStore.getState().turbo).toBe(true);
    expect(screen.getByTestId("header-turbo-chip")).toBeTruthy();
  });

  it("a read-only viewer sees the chip but gets no toggle", () => {
    bindConversationForTest("sess-turbo", { turbo: true });
    renderHeader({ view: "terminal", turboControl: { canToggle: false } });
    expect(screen.getByTestId("header-turbo-chip")).toBeTruthy();
    expect(screen.queryByTestId("header-turbo-chip-trigger")).toBeNull();
  });
});
