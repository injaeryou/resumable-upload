// tus-js-client (Node) -> resumable-upload TusServer.
// Usage: node tus_js_client.mjs <endpoint> <size-in-bytes> [mode]
//   mode = "roundtrip" (default): upload, HEAD, download, SHA compare
//          "terminate": upload, then abort(true) to DELETE, expect 404
import crypto from "node:crypto";
import * as tus from "tus-js-client";

const [endpoint, sizeStr, mode = "roundtrip"] = process.argv.slice(2);
const SIZE = Number(sizeStr);
const data = crypto.randomBytes(SIZE);
const sha = crypto.createHash("sha256").update(data).digest("hex");

function doUpload() {
  return new Promise((resolve, reject) => {
    const upload = new tus.Upload(data, {
      endpoint,
      chunkSize: 256 * 1024,
      retryDelays: [0, 100],
      metadata: { filename: "interop.bin", filetype: "application/octet-stream" },
      onError: reject,
      onSuccess: () => resolve(upload),
    });
    upload.start();
  });
}

const upload = await doUpload();
const url = upload.url;

if (mode === "terminate") {
  // tus-js-client abort(true) issues the TUS termination DELETE.
  await upload.abort(true);
  const head = await fetch(url, { method: "HEAD", headers: { "Tus-Resumable": "1.0.0" } });
  if (head.status !== 404) throw new Error(`expected 404 after terminate, got ${head.status}`);
  console.log("OK terminated " + url);
} else {
  const head = await fetch(url, { method: "HEAD", headers: { "Tus-Resumable": "1.0.0" } });
  if (head.headers.get("upload-offset") !== String(SIZE))
    throw new Error(`bad offset: ${head.headers.get("upload-offset")}`);

  const got = Buffer.from(await (await fetch(url)).arrayBuffer());
  const gotSha = crypto.createHash("sha256").update(got).digest("hex");
  if (gotSha !== sha) throw new Error("sha mismatch on download");
  console.log("OK " + url);
}
