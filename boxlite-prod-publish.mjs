const token = process.env.BOXLITE_TOKEN;
const org = process.env.BOXLITE_ORG_ID;
const box = process.env.BOXLITE_BOX_ID;
if (!token || !org || !box) throw new Error("Missing BoxLite production configuration");
const headers = { authorization: `Bearer ${token}`, "content-type": "application/json" };
const boxBase = `https://api.boxlite.ai/v1/${encodeURIComponent(org)}/boxes/${encodeURIComponent(box)}`;
const publish = await fetch(`${boxBase}/network/inbound`, {
  method: "PUT",
  headers,
  body: JSON.stringify({ mode: "enabled" }),
});
if (!publish.ok) throw new Error(`Publish failed (${publish.status}): ${await publish.text()}`);
const tunnel = await fetch(`${boxBase}/network/tunnel?port=8080`, { method: "POST", headers });
const text = await tunnel.text();
if (!tunnel.ok) throw new Error(`Tunnel failed (${tunnel.status}): ${text}`);
const data = JSON.parse(text);
if (!data.uri) throw new Error(`Tunnel response has no uri: ${text}`);
const check = await fetch(data.uri, { redirect: "manual" });
console.log(JSON.stringify({ public: true, tunnel: data.uri, httpStatus: check.status, location: check.headers.get("location") }));
