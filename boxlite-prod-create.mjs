const token = process.env.BOXLITE_TOKEN;
const prefix = process.env.BOXLITE_ORG_ID;
if (!token || !prefix) throw new Error("Missing BoxLite production configuration");
const base = `https://api.boxlite.ai/api/v1/${prefix}`;
const headers = { authorization: `Bearer ${token}`, "content-type": "application/json" };
const response = await fetch(`${base}/boxes`, {
  method: "POST",
  headers,
  body: JSON.stringify({
    name: "qingjia",
    image: "ghcr.io/boxlite-ai/boxlite-agent-base:v0.1.0",
    network: {
      outbound: { mode: "enabled" },
      inbound: { mode: "enabled" },
    },
    cpus: 1,
    memory_mib: 1024,
    disk_size_gb: 10,
    auto_stop: 900,
    auto_delete: 0,
    auto_resume: true,
  }),
});
const text = await response.text();
if (!response.ok) throw new Error(`Create failed (${response.status}): ${text}`);
console.log(text);
