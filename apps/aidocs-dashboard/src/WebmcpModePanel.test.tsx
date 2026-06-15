// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// DoD #6 panel flow, headless: Local → open modal → log in against codenexus via
// the Tauri HTTP plugin (Rust-backed, mocked here) → switch to WebMCP + render the
// org roster/seats from the API response. The panel uses the plugin fetch (NOT the
// webview's native fetch) precisely so it isn't CORS-blocked at runtime.
const h = vi.hoisted(() => ({ fetch: vi.fn(), invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: h.fetch }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: h.invoke }));

import { WebmcpModePanel } from "./WebmcpModePanel";

const LOGIN_RESPONSE = {
  ok: true,
  user: { id: "u1", email: "claude@codenexus.cloud", name: "Claude (Ops)", role: "SUPER_ADMIN" },
  webmcp: { entitled: true },
  orgs: [
    { id: "org_a", name: "Acme Org", slug: "acme", role: "OWNER", entitled: true, seats: 5, members: 2, isOwner: true },
    { id: "org_b", name: "Beta Org", slug: "beta", role: "MEMBER", entitled: true, seats: 3, members: 4, isOwner: false },
  ],
};

beforeEach(() => {
  window.localStorage.clear();
  h.fetch.mockReset();
  h.invoke.mockReset();
});
afterEach(() => cleanup());

describe("WebmcpModePanel", () => {
  it("starts in Local mode with no stored session", () => {
    render(<WebmcpModePanel />);
    expect(screen.getByText("Local")).toBeTruthy();
  });

  it("logs in via the Tauri HTTP plugin (not native fetch) and renders org roster + seats", async () => {
    h.fetch.mockResolvedValue({ ok: true, status: 200, json: async () => LOGIN_RESPONSE });

    render(<WebmcpModePanel />);
    fireEvent.click(screen.getByText("Local"));
    expect(screen.getByText("WebMCP access")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("Email"), { target: { value: "claude@codenexus.cloud" } });
    fireEvent.change(screen.getByPlaceholderText("Password"), { target: { value: "secret-pw" } });
    fireEvent.click(screen.getByText("Log in to WebMCP"));

    // it called the codenexus login endpoint THROUGH the plugin (Rust-backed)
    await waitFor(() => expect(h.fetch).toHaveBeenCalled());
    const [url, opts] = h.fetch.mock.calls[0];
    expect(String(url)).toContain("https://codenexus.cloud/api/dashboard/login");
    expect(JSON.parse(opts.body)).toMatchObject({ email: "claude@codenexus.cloud", password: "secret-pw" });

    // signed-in view renders the user's orgs (multi-org) from the API response
    await waitFor(() => expect(screen.getByText("Your organizations")).toBeTruthy());
    expect(screen.getByText("Acme Org")).toBeTruthy();
    expect(screen.getByText("Beta Org")).toBeTruthy();
    expect(screen.getByText(/2 members · 5 seats/)).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem("aidocs.webmcp.session") || "{}").user.email).toBe(
      "claude@codenexus.cloud",
    );
  });

  it("Continue with Google runs the browser+loopback handoff and signs in", async () => {
    let capturedState = "";
    h.invoke.mockImplementation(async (cmd: string, args: any) => {
      if (cmd === "open_url") {
        capturedState = new URL(args.url).searchParams.get("state") || "";
        return null;
      }
      if (cmd === "webmcp_oauth_capture") {
        return await new Promise((r) => setTimeout(() => r("social-code|" + capturedState), 10));
      }
      return null;
    });
    h.fetch.mockImplementation(async (url: string) => {
      if (String(url).includes("/api/dashboard/social-exchange")) {
        return { ok: true, status: 200, json: async () => LOGIN_RESPONSE };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });

    render(<WebmcpModePanel />);
    fireEvent.click(screen.getByText("Local"));
    fireEvent.click(screen.getByText("Google"));

    // started the loopback listener + opened the codenexus bridge for google
    await waitFor(() =>
      expect(h.invoke).toHaveBeenCalledWith("webmcp_oauth_capture", expect.objectContaining({ port: 8765 })),
    );
    const openCall = h.invoke.mock.calls.find((c) => c[0] === "open_url");
    expect(openCall![1].url).toContain("/dashboard-auth?provider=google");
    // exchanged the loopback code → signed in (orgs list renders)
    await waitFor(() => expect(screen.getByText("Your organizations")).toBeTruthy());
    const exCall = h.fetch.mock.calls.find((c) => String(c[0]).includes("/api/dashboard/social-exchange"));
    expect(JSON.parse(exCall![1].body).code).toBe("social-code");
  });

  it("surfaces an invalid-credentials error without entering WebMCP", async () => {
    h.fetch.mockResolvedValue({ ok: false, status: 401, json: async () => ({ error: "Invalid credentials" }) });

    render(<WebmcpModePanel />);
    fireEvent.click(screen.getByText("Local"));
    fireEvent.change(screen.getByPlaceholderText("Email"), { target: { value: "x@y.z" } });
    fireEvent.change(screen.getByPlaceholderText("Password"), { target: { value: "nope" } });
    fireEvent.click(screen.getByText("Log in to WebMCP"));

    await waitFor(() => expect(screen.getByText("Invalid credentials")).toBeTruthy());
    expect(window.localStorage.getItem("aidocs.webmcp.session")).toBeNull();
  });
});
