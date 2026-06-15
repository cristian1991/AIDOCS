// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

// Drives the DoD #6 client orchestration headlessly: PKCE + loopback capture
// (Rust command, mocked) + browser open + token exchange + project_list, all the
// way to parsed projects. The native loopback + HTTPS transport are mocked at the
// Tauri boundary (invoke / plugin-http fetch); the real ones need a Tauri runtime.

const h = vi.hoisted(() => {
  let capturedState = "";
  const invoke = vi.fn(async (cmd: string, args: any) => {
    if (cmd === "open_url") {
      capturedState = new URL(args.url).searchParams.get("state") || "";
      return null;
    }
    if (cmd === "webmcp_oauth_capture") {
      // resolves AFTER open_url has set the state (mirrors the real flow:
      // listener started, browser opened, then the redirect arrives)
      return await new Promise((res) => setTimeout(() => res("test-code|" + capturedState), 10));
    }
    return null;
  });
  const fetch = vi.fn(async (url: string, _opts: any) => {
    if (String(url).includes("/oauth/token")) {
      return { ok: true, status: 200, json: async () => ({ access_token: "ogt_test", scope: "catalog tier_r_invoke" }) };
    }
    if (String(url).includes("/v1/mcp")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          result: {
            content: [
              {
                type: "text",
                text: JSON.stringify({
                  projects: [{ project_id: "p1", name: "AutoDeployBase", source: "local", is_default: true, current: true }],
                }),
              },
            ],
          },
        }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });
  return { invoke, fetch };
});

vi.mock("@tauri-apps/api/core", () => ({ invoke: h.invoke }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: h.fetch }));

import { connectAndListProjects } from "./WebmcpProjects";

afterEach(() => vi.clearAllMocks());

describe("connectAndListProjects (DoD #6 client)", () => {
  it("runs OAuth (loopback + browser) then lists projects via the gate API", async () => {
    const projects = await connectAndListProjects();
    expect(projects).toEqual([
      { project_id: "p1", name: "AutoDeployBase", source: "local", is_default: true, current: true },
    ]);
    // started the loopback listener on the registered port + opened the gate authorize URL with PKCE
    expect(h.invoke).toHaveBeenCalledWith("webmcp_oauth_capture", expect.objectContaining({ port: 8765 }));
    const openCall = h.invoke.mock.calls.find((c) => c[0] === "open_url");
    expect(openCall![1].url).toContain("/oauth/authorize");
    expect(openCall![1].url).toContain("code_challenge_method=S256");
    expect(openCall![1].url).toContain("client_id=ogcid_");
    // token exchange + project_list both hit the gate; project_list carries the bearer
    const urls = h.fetch.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes("/oauth/token"))).toBe(true);
    const mcpCall = h.fetch.mock.calls.find((c) => String(c[0]).includes("/v1/mcp"));
    expect(mcpCall![1].headers.Authorization).toBe("Bearer ogt_test");
    expect(JSON.parse(mcpCall![1].body).params.name).toBe("project_list");
  });

  it("aborts on OAuth state mismatch (CSRF guard)", async () => {
    h.invoke.mockImplementationOnce(async () => "code|TAMPERED_STATE"); // capture returns wrong state
    await expect(connectAndListProjects()).rejects.toThrow(/state mismatch/i);
  });
});
