import { describe, expect, it } from "vitest";

import {
  nativeAgentHasCapability,
  nativeCodingAgentForHarness,
} from "./nativeCodingAgents";

describe("Codex native model selection", () => {
  it("exposes the live Codex model catalog alongside approval controls", () => {
    expect(nativeCodingAgentForHarness("codex-native")?.capabilities).toEqual([
      "approvalMode",
      "modelPicker",
    ]);
    expect(
      nativeAgentHasCapability(
        { name: "codex-native-ui", harness: "codex-native" },
        "modelPicker",
      ),
    ).toBe(true);
  });
});
