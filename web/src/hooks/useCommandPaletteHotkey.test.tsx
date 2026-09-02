import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isCommandPaletteHotkey, useCommandPaletteHotkey } from "./useCommandPaletteHotkey";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function event(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

function press(init: KeyboardEventInit): KeyboardEvent {
  const e = event(init);
  window.dispatchEvent(e);
  return e;
}

describe("isCommandPaletteHotkey", () => {
  it("uses Cmd on macOS and Ctrl on other platforms", () => {
    expect(isCommandPaletteHotkey(event({ key: "k", metaKey: true }), true)).toBe(true);
    expect(isCommandPaletteHotkey(event({ key: "k", ctrlKey: true }), true)).toBe(false);
    expect(isCommandPaletteHotkey(event({ key: "k", ctrlKey: true }), false)).toBe(true);
    expect(isCommandPaletteHotkey(event({ key: "k", metaKey: true }), false)).toBe(false);
    // Uppercase (some layouts report "K" with the modifier).
    expect(isCommandPaletteHotkey(event({ key: "K", metaKey: true }), true)).toBe(true);
  });

  it("rejects plain k, and k with Alt or Shift held", () => {
    expect(isCommandPaletteHotkey(event({ key: "k" }), true)).toBe(false);
    expect(isCommandPaletteHotkey(event({ key: "k", metaKey: true, altKey: true }), true)).toBe(
      false,
    );
    expect(isCommandPaletteHotkey(event({ key: "k", ctrlKey: true, shiftKey: true }), false)).toBe(
      false,
    );
  });

  it("rejects other keys with the modifier", () => {
    expect(isCommandPaletteHotkey(event({ key: "j", metaKey: true }), true)).toBe(false);
  });
});

describe("useCommandPaletteHotkey", () => {
  it("toggles on Cmd+K and prevents the browser default", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle, true, true));

    const e = press({ key: "k", metaKey: true });

    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("leaves Ctrl+K alone on macOS (emacs kill-to-end-of-line keeps working)", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle, true, true));

    const e = press({ key: "k", ctrlKey: true });

    expect(onToggle).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("fires on Ctrl+K on Windows/Linux", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle, true, false));

    const e = press({ key: "k", ctrlKey: true });

    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("ignores auto-repeat", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle, true, true));

    press({ key: "k", metaKey: true, repeat: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("does nothing when disabled", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle, false, true));

    const e = press({ key: "k", metaKey: true });

    expect(onToggle).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("bails when focus sits inside a terminal or code editor", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle, true, true));

    const term = document.createElement("div");
    term.className = "xterm";
    const input = document.createElement("input");
    term.appendChild(input);
    document.body.appendChild(term);
    input.focus();
    expect(document.activeElement).toBe(input);

    press({ key: "k", metaKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("unbinds on unmount", () => {
    const onToggle = vi.fn();
    const { unmount } = renderHook(() => useCommandPaletteHotkey(onToggle, true, true));
    unmount();

    press({ key: "k", metaKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });
});
