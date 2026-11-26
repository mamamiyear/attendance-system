const token = process.env.BOXLITE_TOKEN;
const org = process.env.BOXLITE_ORG_ID;
const box = process.env.BOXLITE_BOX_ID;
const apiRoot = (process.env.BOXLITE_API_ROOT || "https://api.dev.boxlite.ai").replace(/\/$/, "");
const port = Number(process.env.BOXLITE_TUNNEL_PORT || 8080);

if (!token || !org || !box) {
  throw new Error("Missing BoxLite tunnel configuration");
}
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 22222) {
  throw new Error(`Invalid tunnel port: ${port}`);
}

const headers = {
  authorization: `Bearer ${token}`,
  "content-type": "application/json",
};
const boxBase = `${apiRoot}/v1/${encodeURIComponent(org)}/boxes/${encodeURIComponent(box)}`;

// Ensure the prepared tunnel is publicly reachable. This call is idempotent.
const inbound = await fetch(`${boxBase}/network/inbound`, {
  method: "PUT",
  headers,
  body: JSON.stringify({ mode: "enabled" }),
});
if (!inbound.ok) {
  throw new Error(`Enable inbound failed (${inbound.status}): ${await inbound.text()}`);
}

// Preparing a tunnel is safe to repeat and returns the existing descriptor
// when a route has already been provisioned for this box and port.
const response = await fetch(`${boxBase}/network/tunnel?port=${port}`, {
  method: "POST",
  headers,
});
const text = await response.text();
if (!response.ok) {
  throw new Error(`Prepare tunnel failed (${response.status}): ${text}`);
}
const descriptor = JSON.parse(text);
if (typeof descriptor.uri !== "string" || !descriptor.uri) {
  throw new Error(`Tunnel response has no uri: ${text}`);
}

const check = await fetch(descriptor.uri, { redirect: "manual" });
console.log(JSON.stringify({ box, port, tunnel: descriptor.uri, httpStatus: check.status }));
