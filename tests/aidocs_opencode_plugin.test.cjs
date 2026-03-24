const assert = require("node:assert/strict")
const fs = require("node:fs/promises")
const os = require("node:os")
const path = require("node:path")
const test = require("node:test")

const { AIDOCSPlugin } = require("../core/plugins/aidocs.js")

async function seedProject(root, { managed = false, workflowActions = [] } = {}) {
  await fs.mkdir(path.join(root, ".MEMORY", ".aidocs"), { recursive: true })
  await fs.writeFile(path.join(root, ".MEMORY", ".aidocs", "index.aidocs"), "# index\n", "utf8")
  await fs.writeFile(path.join(root, "AGENTS.md"), "routing\n", "utf8")
  if (managed) {
    await fs.mkdir(path.join(root, ".MEMORY", "config"), { recursive: true })
    await fs.writeFile(
      path.join(root, ".MEMORY", "config", "aidocs-managed.json"),
      JSON.stringify({ active: true, session_id: "session-1" }, null, 2),
      "utf8",
    )
  }
  if (workflowActions.length) {
    await fs.mkdir(path.join(root, ".MEMORY", "config"), { recursive: true })
    await fs.writeFile(
      path.join(root, ".MEMORY", "config", "workflow-actions.json"),
      JSON.stringify({ actions: workflowActions }, null, 2),
      "utf8",
    )
  }
}

async function createPlugin(root) {
  return AIDOCSPlugin({
    project: {},
    client: {},
    directory: root,
    worktree: root,
    serverUrl: new URL("http://localhost"),
    $: {},
  })
}

test("unmanaged initialized project injects /aidocs guidance and blocks guarded tools", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "aidocs-opencode-"))
  await seedProject(root)
  const plugin = await createPlugin(root)

  const messageOutput = { message: {}, parts: [{ text: "fix this bug" }] }
  await plugin["chat.message"]({ sessionID: "s1" }, messageOutput)

  const systemOutput = { system: [] }
  await plugin["experimental.chat.system.transform"]({ sessionID: "s1", model: {} }, systemOutput)
  assert.match(systemOutput.system.join("\n"), /run `\/aidocs` first/i)

  await assert.rejects(
    () => plugin["tool.execute.before"]({ tool: "read", sessionID: "s1", callID: "c1" }, { args: {} }),
    /Run \/aidocs first/i,
  )
})

test("managed project injects session and workflow context and sets shell env", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "aidocs-opencode-"))
  await seedProject(root, {
    managed: true,
    workflowActions: [{ trigger: "after_git_push", kind: "github_workflow_check" }],
  })
  const plugin = await createPlugin(root)

  const messageOutput = { message: {}, parts: [{ text: "continue the task" }] }
  await plugin["chat.message"]({ sessionID: "s2" }, messageOutput)

  const systemOutput = { system: [] }
  await plugin["experimental.chat.system.transform"]({ sessionID: "s2", model: {} }, systemOutput)
  const systemText = systemOutput.system.join("\n")
  assert.match(systemText, /AIDOCS-managed mode is active/i)
  assert.match(systemText, /session-1/)
  assert.match(systemText, /github_workflow_check/)
  assert.match(systemText, /fallback only/i)

  const envOutput = { env: {} }
  await plugin["shell.env"]({ cwd: root, sessionID: "s2" }, envOutput)
  assert.equal(envOutput.env.AIDOCS_INITIALIZED, "1")
  assert.equal(envOutput.env.AIDOCS_MANAGED_MODE, "1")
  assert.equal(envOutput.env.AIDOCS_SESSION_ID, "session-1")
})

test("aidocs command temporarily bypasses unmanaged tool blocking", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "aidocs-opencode-"))
  await seedProject(root)
  const plugin = await createPlugin(root)

  await plugin["command.execute.before"]({ command: "aidocs", sessionID: "s3", arguments: "" }, { parts: [] })
  await plugin["tool.execute.before"]({ tool: "read", sessionID: "s3", callID: "c3" }, { args: {} })

  await plugin.event({ event: { type: "command.executed", properties: { name: "aidocs", sessionID: "s3", arguments: "", messageID: "m1" } } })

  await assert.rejects(
    () => plugin["tool.execute.before"]({ tool: "read", sessionID: "s3", callID: "c4" }, { args: {} }),
    /Run \/aidocs first/i,
  )
})
