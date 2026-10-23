import { describe, expect, it } from "vitest";
import { modelPickerKindForConv } from "@/pages/ChatPage";
import { isCodexTurboSession } from "./TurboControls";

// The header decides Turbo applicability with `isCodexTurboSession`; the
// composer with `modelPickerKindForConv(...) === "codex"`. They must agree, or
// a session could show the chip in one view and not the other.
describe("isCodexTurboSession", () => {
  const cases: [string, Parameters<typeof isCodexTurboSession>[0]][] = [
    ["Codex native wrapper", { labels: { "omnigent.wrapper": "codex-native-ui" } }],
    [
      "Codex wrapper beats a stale harness",
      { labels: { "omnigent.wrapper": "codex-native-ui" }, harness: "claude-native" },
    ],
    ["Codex sub-agent wrapper", { labels: { "omnigent.wrapper": "codex-native-ui-subagent" } }],
    ["Claude native wrapper", { labels: { "omnigent.wrapper": "claude-code-native-ui" } }],
    [
      "another wrapper with a codex-native harness",
      { labels: { "omnigent.wrapper": "cursor-native-ui" }, harness: "codex-native" },
    ],
    ["label-less codex-native harness", { labels: {}, harness: "codex-native" }],
    [
      "null wrapper, codex-native harness",
      { labels: { "omnigent.wrapper": null }, harness: "codex-native" },
    ],
    ["label-less SDK codex harness", { labels: {}, harness: "codex" }],
    ["label-less claude-sdk harness", { labels: {}, harness: "claude-sdk" }],
    ["no labels, no harness", {}],
    ["no session", null],
  ];

  it.each(cases)("%s agrees with the composer's Codex gate", (_name, conv) => {
    expect(isCodexTurboSession(conv)).toBe(modelPickerKindForConv(conv) === "codex");
  });

  it("is true only for the Codex wrapper and label-less codex-native sessions", () => {
    expect(cases.filter(([, conv]) => isCodexTurboSession(conv)).map(([name]) => name)).toEqual([
      "Codex native wrapper",
      "Codex wrapper beats a stale harness",
      "label-less codex-native harness",
      "null wrapper, codex-native harness",
    ]);
  });
});
