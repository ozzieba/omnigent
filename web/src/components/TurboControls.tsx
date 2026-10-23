import { Switch } from "@/components/ui/switch";
import { showToast } from "@/components/ui/toast";
import { WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

/** Shown beside the Turbo controls: what the switch allows, in one line. */
export const TURBO_DESCRIPTION =
  "Let this session's Codex turns use paid credits past the subscription margin";

/** The Turbo chip's hover title, and the base of its accessible name. */
export const TURBO_CHIP_TITLE = "Turbo is on: this session may spend paid credits";

/**
 * Whether a session's per-session Turbo switch applies: the Codex native-UI
 * wrapper, or a label-less session resolved to the codex-native harness. Any
 * other wrapper label (sub-agent variants included) is authoritative and rules
 * it out. Mirrors ChatPage's `modelPickerKindForConv(conv) === "codex"` — the
 * composer's gate — for surfaces outside ChatPage, such as the header.
 *
 * :param conv: Session snapshot or sidebar row carrying labels and harness.
 * :returns: True only for a Codex session.
 */
export function isCodexTurboSession(
  conv:
    { labels?: Record<string, string | null> | null; harness?: string | null } | null | undefined,
): boolean {
  const wrapper = conv?.labels?.[WRAPPER_LABEL_KEY];
  if (wrapper != null) return wrapper === "codex-native-ui";
  return conv?.harness === "codex-native";
}

/**
 * Persistent paid-credits indicator. Turbo lets the llmq gate admit a turn that
 * would spend credits, so wherever it is on it must be readable at a glance,
 * not only in a hover tooltip. Presentational only: callers decide when Turbo
 * applies and is on.
 */
export function TurboChip({ testId, className }: { testId: string; className?: string }) {
  return (
    <span
      data-testid={testId}
      title={TURBO_CHIP_TITLE}
      className={cn(
        "shrink-0 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-xs font-medium leading-none text-amber-700 dark:text-amber-400",
        className,
      )}
    >
      Turbo: paid credits
    </span>
  );
}

/**
 * The per-session Turbo switch (oz, 2026-10-06). Writes through the store
 * immediately (optimistic, rolled back on a failed PATCH) rather than through a
 * modal's Save: it is a plain session flag, not part of the launch config. A
 * failed write is announced, not just rolled back — Turbo decides whether the
 * session may spend paid credits, and a silent snap-back is easy to miss.
 */
export function TurboSwitch({ testId, componentId }: { testId: string; componentId: string }) {
  const turbo = useChatStore((s) => s.turbo);
  const setTurbo = useChatStore((s) => s.setTurbo);
  return (
    <div className="flex items-center gap-2">
      <Switch
        checked={turbo}
        onCheckedChange={(checked) => {
          const conversationId = useChatStore.getState().conversationId;
          void setTurbo(checked).catch(() => {
            // The store already rolled back. Name the state the session is
            // left in; if the user has since switched away, the toggle's
            // starting value is the best answer.
            const s = useChatStore.getState();
            const stillOn = s.conversationId === conversationId ? s.turbo : !checked;
            showToast(`Couldn't change Turbo — it is still ${stillOn ? "On" : "Off"}`);
          });
        }}
        aria-label="Turbo"
        data-testid={testId}
        componentId={componentId}
      />
      <span className="text-sm text-muted-foreground" data-testid={`${testId}-state`}>
        {turbo ? "On: paid credits allowed" : "Off"}
      </span>
    </div>
  );
}
