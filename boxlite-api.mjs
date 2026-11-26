const base = process.env.BOXLITE_BASE;
const token = process.env.BOXLITE_TOKEN;

if (!base || !token) {
  throw new Error("Missing BOXLITE_BASE or BOXLITE_TOKEN");
}

const authHeaders = { authorization: `Bearer ${token}` };
const command = process.argv.slice(2).join(" ");
if (!command) throw new Error("Missing shell command");

const started = await fetch(`${base}/exec`, {
  method: "POST",
  headers: { ...authHeaders, "content-type": "application/json" },
  body: JSON.stringify({
    command: "sh",
    args: ["-c", command],
    env: {},
    tty: false,
    ...(process.env.BOXLITE_EXEC_USER
      ? { user: process.env.BOXLITE_EXEC_USER }
      : {}),
  }),
});

const startedText = await started.text();
if (!started.ok) {
  throw new Error(`Exec failed (${started.status}): ${startedText}`);
}

const { execution_id: executionId } = JSON.parse(startedText);
for (let attempt = 0; attempt < 30; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 500));
  const statusResponse = await fetch(`${base}/executions/${executionId}`, {
    headers: authHeaders,
  });
  const status = await statusResponse.json();
  if (status.status !== "running" && status.status !== "pending") {
    console.log(JSON.stringify(status));
    process.exit(status.exit_code ?? 1);
  }
}

throw new Error("Execution status timed out");
