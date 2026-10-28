import type { Meta, StoryObj } from "@storybook/react-vite";
import { OwnerContinuationPanel } from "./OwnerContinuationPanel";

const state = {
  revision: 14,
  guarded: true,
  status: "waiting",
  wait_id: "0123456789abcdef0123456789abcdef",
  automatic: {
    schema_version: 1,
    status: "awaiting_authorization",
    remaining_attempts: 2,
    remaining_booked_ppm: 125000,
    expires_at_epoch: 1793210400,
    next_retry_epoch: 1793209500,
  },
};

const meta = {
  title: "Components/Docloop/Owner continuation",
  component: OwnerContinuationPanel,
  args: {
    sessionId: "demo-session",
    bindingId: "demo-document-binding",
    active: true,
  },
} satisfies Meta<typeof OwnerContinuationPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

export const AwaitingOwnerAuthorization: Story = {
  beforeEach: () => {
    const originalFetch = window.fetch;
    window.fetch = async (input, init) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      if (url.endsWith("/owner-continuation/automatic")) {
        return new Response(
          JSON.stringify({ request_id: "demo-request", status: "queued", action: "automatic" }),
          {
            status: 202,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      if (url.endsWith("/owner-continuation")) {
        return new Response(JSON.stringify(state), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return originalFetch(input, init);
    };
    return () => {
      window.fetch = originalFetch;
    };
  },
};
