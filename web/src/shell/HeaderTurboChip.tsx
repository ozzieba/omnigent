import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  TURBO_CHIP_TITLE,
  TURBO_DESCRIPTION,
  TurboChip,
  TurboSwitch,
} from "@/components/TurboControls";
import { useChatStore } from "@/store/chatStore";
import { useTerminalFirst } from "./TerminalFirstContext";

/**
 * The Turbo chip for a terminal-first Codex session's Terminal view. The
 * composer carries the chip in Chat view but unmounts while the terminal owns
 * the main surface, which would leave a session that may spend paid credits
 * with no visible Turbo state. The header chip takes over exactly then (the
 * same view condition ChatPage uses to swap the chat surface for the terminal).
 *
 * Editors get the switch in a popover, so Turbo can be turned off without
 * leaving the terminal; read-only viewers see the plain chip. The caller
 * renders this only for a Codex session (see `isCodexTurboSession`).
 */
export function HeaderTurboChip({ canToggle }: { canToggle: boolean }) {
  const terminalFirst = useTerminalFirst();
  const turbo = useChatStore((s) => s.turbo);
  if (!turbo || terminalFirst?.isTerminalFirst !== true || terminalFirst.view !== "terminal") {
    return null;
  }
  if (!canToggle) return <TurboChip testId="header-turbo-chip" className="max-md:mx-1.5" />;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`${TURBO_CHIP_TITLE}. Change Turbo`}
          data-testid="header-turbo-chip-trigger"
          className="shrink-0 rounded-full max-md:mx-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
        >
          <TurboChip testId="header-turbo-chip" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" data-testid="header-turbo-popover">
        <div className="flex flex-col gap-0.5">
          <span className="font-medium">Turbo</span>
          <span className="text-sm text-muted-foreground">{TURBO_DESCRIPTION}</span>
        </div>
        <TurboSwitch testId="header-turbo-switch" componentId="chat.header.turbo" />
      </PopoverContent>
    </Popover>
  );
}
