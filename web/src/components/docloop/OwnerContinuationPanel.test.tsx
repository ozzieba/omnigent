import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OwnerContinuationPanel } from "./OwnerContinuationPanel";

const fetcher = vi.hoisted(() => ({ authenticated: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetcher.authenticated }));

const waiting = {
  revision: 7,
  guarded: true,
  status: "waiting",
  wait_id: "0123456789abcdef0123456789abcdef",
  automatic: {
    status: "awaiting_authorization",
    remaining_attempts: 3,
    remaining_booked_ppm: 180000,
  },
};

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

beforeEach(() => {
  fetcher.authenticated
    .mockReset()
    .mockImplementation(async (_url: string, options?: RequestInit) => {
      if (options?.method === "POST")
        return response({ request_id: "queued-id", status: "queued" }, 202);
      return response(waiting);
    });
});

describe("owner continuation controls", () => {
  it("queues explicit start intent with the bound document and wait revision", async () => {
    render(<OwnerContinuationPanel sessionId="s 1" bindingId="binding-1" active />);
    const start = await screen.findByRole("button", { name: "Queue automatic recovery" });
    expect(screen.queryByRole("button", { name: "Queue stop for future retries" })).toBeNull();
    fireEvent.click(start);
    await screen.findByText(/supervisor must authorize/);
    expect(
      await screen.findByRole("button", { name: "Queue stop for future retries" }),
    ).toBeVisible();
    const [url, requestInit] = fetcher.authenticated.mock.calls.findLast(
      ([, opts]) => opts?.method === "POST",
    ) as [string, RequestInit];
    expect(url).toBe("/v1/sessions/s%201/docloop/owner-continuation/automatic");
    expect(requestInit.method).toBe("POST");
    expect(requestInit.headers).toEqual({
      "Content-Type": "application/json",
      "x-docloop-edit": "1",
    });
    expect(JSON.parse(requestInit.body as string)).toEqual({
      binding_id: "binding-1",
      revision: 7,
      wait_id: "0123456789abcdef0123456789abcdef",
    });
  });

  it("sends at most one start request while the first request is pending", async () => {
    let finish!: (response: Response) => void;
    fetcher.authenticated.mockImplementation((_url: string, options?: RequestInit) => {
      if (options?.method === "POST")
        return new Promise((resolve) => {
          finish = resolve;
        });
      return Promise.resolve(response(waiting));
    });
    render(<OwnerContinuationPanel sessionId="s" bindingId="b" active />);
    const start = await screen.findByRole("button", { name: "Queue automatic recovery" });
    fireEvent.click(start);
    fireEvent.click(start);
    const posts = fetcher.authenticated.mock.calls.filter(
      ([, options]) => options?.method === "POST",
    );
    expect(posts).toHaveLength(1);
    await act(async () => finish(response({ status: "queued" }, 202)));
  });

  it.each([
    { name: "unguarded", state: { ...waiting, guarded: false } },
    { name: "not waiting", state: { ...waiting, status: "idle" } },
    { name: "invalid wait id", state: { ...waiting, wait_id: "stale" } },
    { name: "disabled policy", state: { ...waiting, automatic: { status: "disabled" } } },
  ])("fails closed for $name", async ({ state }) => {
    fetcher.authenticated.mockResolvedValueOnce(response(state));
    render(<OwnerContinuationPanel sessionId="s" bindingId="b" active />);
    await screen.findByText(/Automatic recovery/);
    expect(screen.queryByRole("button", { name: "Queue automatic recovery" })).toBeNull();
  });

  it("labels a hold as requiring owner review and offers a stop for future retries", async () => {
    fetcher.authenticated.mockResolvedValueOnce(
      response({ ...waiting, automatic: { status: "ambiguous_hold" } }),
    );
    render(<OwnerContinuationPanel sessionId="s" bindingId="b" active />);
    expect(
      await screen.findByText("Owner review is required before this can continue."),
    ).toBeVisible();
  });

  it("offers a future retry stop while authorization is held", async () => {
    fetcher.authenticated.mockResolvedValueOnce(
      response({ ...waiting, automatic: { status: "held" } }),
    );
    render(<OwnerContinuationPanel sessionId="s" bindingId="b" active />);
    const cancel = await screen.findByRole("button", {
      name: "Queue stop for future retries",
    });
    fireEvent.click(cancel);
    await screen.findByText(/A retry already in progress may finish/);
    const [, requestInit] = fetcher.authenticated.mock.calls.findLast(
      ([, opts]) => opts?.method === "POST",
    ) as [string, RequestInit];
    expect(JSON.parse(requestInit.body as string)).toEqual({ binding_id: "b" });
  });

  it("polls status while active and stops polling when inactive", async () => {
    vi.useFakeTimers();
    const view = render(<OwnerContinuationPanel sessionId="s" bindingId="b" active />);
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(fetcher.authenticated).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(fetcher.authenticated).toHaveBeenCalledTimes(2);
    view.rerender(<OwnerContinuationPanel sessionId="s" bindingId="b" active={false} />);
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetcher.authenticated).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});
