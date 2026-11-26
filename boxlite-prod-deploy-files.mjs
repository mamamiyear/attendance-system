import { readFile } from "node:fs/promises";

const token = process.env.BOXLITE_TOKEN;
const org = process.env.BOXLITE_ORG_ID;
const box = process.env.BOXLITE_BOX_ID;
const appOrigin = process.env.BOXLITE_APP_ORIGIN;
if (!token || !org || !box || !appOrigin) throw new Error("Missing production deployment configuration");

const api = `https://api.boxlite.ai/v1/${encodeURIComponent(org)}/boxes/${encodeURIComponent(box)}`;
const auth = { authorization: `Bearer ${token}` };

function writeOctal(header, offset, length, value) {
  const text = Math.trunc(value).toString(8).padStart(length - 1, "0") + "\0";
  header.write(text, offset, length, "ascii");
}

function tarEntry(name, content, mode = 0o644) {
  const body = Buffer.isBuffer(content) ? content : Buffer.from(content);
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, "utf8");
  writeOctal(header, 100, 8, mode);
  writeOctal(header, 108, 8, 0);
  writeOctal(header, 116, 8, 0);
  writeOctal(header, 124, 12, body.length);
  writeOctal(header, 136, 12, Math.floor(Date.now() / 1000));
  header.fill(0x20, 148, 156);
  header.write("0", 156, 1, "ascii");
  header.write("ustar\0", 257, 6, "ascii");
  header.write("00", 263, 2, "ascii");
  writeOctal(header, 148, 8, [...header].reduce((sum, byte) => sum + byte, 0));
  const padding = Buffer.alloc((512 - (body.length % 512)) % 512);
  return Buffer.concat([header, body, padding]);
}

const sourceFiles = [
  ["./boxlite-app/index.html", "qingjia-app/index.html"],
  ["./boxlite-app/server.py", "qingjia-app/server.py"],
  ["./boxlite-app/verify_workflow.py", "qingjia-app/verify_workflow.py"],
  ["./boxlite-app/verify_employee_admin.py", "qingjia-app/verify_employee_admin.py"],
  ["./qingjia-sleep-wrapper", "qingjia-sleep-wrapper", 0o755],
  ["./qingjia-supervisor", "qingjia-supervisor", 0o755],
];
const entries = [];
for (const [local, remote, mode] of sourceFiles) {
  entries.push(tarEntry(remote, await readFile(new URL(local, import.meta.url)), mode));
}
let env = await readFile(new URL("./boxlite-app/.env", import.meta.url), "utf8");
env = env.replace(/^APP_ORIGIN=.*$/m, `APP_ORIGIN=${appOrigin}`);
if (!env.includes("APP_ORIGIN=")) env = `${env.trim()}\nAPP_ORIGIN=${appOrigin}\n`;
entries.push(tarEntry("qingjia-app/.env", env, 0o600));
entries.push(Buffer.alloc(1024));
const archive = Buffer.concat(entries);

await fetch(`${api}/start`, { method: "POST", headers: auth });
let upload;
for (let attempt = 0; attempt < 12; attempt += 1) {
  upload = await fetch(`${api}/files?path=%2Fhome%2Fboxlite&overwrite=true`, {
    method: "PUT",
    headers: { ...auth, "content-type": "application/x-tar" },
    body: archive,
  });
  if (upload.ok || upload.status !== 409 || attempt === 11) break;
  await new Promise((resolve) => setTimeout(resolve, 1500));
}
if (!upload.ok) throw new Error(`Upload failed (${upload.status}): ${await upload.text()}`);

async function exec(command) {
  let response;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    response = await fetch(`${api}/exec`, {
      method: "POST",
      headers: { ...auth, "content-type": "application/json" },
      body: JSON.stringify({ command: "sh", args: ["-c", command], env: {}, tty: false }),
    });
    if (response.ok || response.status !== 404 || attempt === 19) break;
    await fetch(`${api}/start`, { method: "POST", headers: auth });
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  if (!response.ok) throw new Error(`Exec failed (${response.status}): ${await response.text()}`);
  const { execution_id: id } = await response.json();
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    const status = await (await fetch(`${api}/executions/${id}`, { headers: auth })).json();
    if (!["running", "pending"].includes(status.status)) {
      if (status.exit_code !== 0) throw new Error(`Remote command failed: ${JSON.stringify(status)}`);
      return status;
    }
  }
  throw new Error("Remote command timed out");
}

await exec("mkdir -p /home/boxlite/qingjia-app/data/attachments; python3 -m py_compile /home/boxlite/qingjia-app/server.py; chmod 600 /home/boxlite/qingjia-app/.env; chmod 755 /home/boxlite/qingjia-sleep-wrapper /home/boxlite/qingjia-supervisor; if [ ! -x /usr/bin/sleep.qingjia-original ]; then sudo cp /usr/bin/sleep /usr/bin/sleep.qingjia-original; fi; sudo cp /home/boxlite/qingjia-sleep-wrapper /usr/bin/sleep.qingjia-new; sudo chmod 755 /usr/bin/sleep.qingjia-new; sudo mv -f /usr/bin/sleep.qingjia-new /usr/bin/sleep; ps -eo pid=,comm=,args= | awk '$2 == \"sh\" && $3 == \"/bin/sh\" && $4 == \"/home/boxlite/qingjia-supervisor\" {print $1}' | xargs -r kill; ps -eo pid=,comm=,args= | awk '$2 ~ /^python/ && $3 == \"python3\" && $4 == \"/home/boxlite/qingjia-app/server.py\" {print $1}' | xargs -r kill; rm -rf /tmp/qingjia-supervisor.lock; /usr/bin/sleep.qingjia-original 1; nohup /bin/sh /home/boxlite/qingjia-supervisor >/tmp/qingjia-supervisor.bootstrap.log 2>&1 </dev/null &");
await exec("for i in 1 2 3 4 5 6 7 8; do /usr/bin/sleep.qingjia-original 1; curl -fsS http://127.0.0.1:8080/health && exit 0; done; tail -n 80 /home/boxlite/qingjia-app/data/service.log; exit 1");
await exec("old_pid=$(ps -eo pid=,comm=,args= | awk '$2 ~ /^python/ && $3 == \"python3\" && $4 == \"/home/boxlite/qingjia-app/server.py\" {print $1; exit}'); test -n \"$old_pid\"; kill \"$old_pid\"; for i in 1 2 3 4 5 6 7 8; do /usr/bin/sleep.qingjia-original 1; new_pid=$(ps -eo pid=,comm=,args= | awk '$2 ~ /^python/ && $3 == \"python3\" && $4 == \"/home/boxlite/qingjia-app/server.py\" {print $1; exit}'); if [ -n \"$new_pid\" ] && [ \"$new_pid\" != \"$old_pid\" ] && curl -fsS http://127.0.0.1:8080/health >/dev/null; then exit 0; fi; done; exit 1");
console.log(JSON.stringify({ deployed: true, box, appOrigin, uploadedBytes: archive.length, autoRestartVerified: true }));
