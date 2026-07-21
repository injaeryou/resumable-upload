// tus-js-client (Node) -> resumable-upload TusServer.
// Usage: node tus_js_client.mjs <endpoint> <size-in-bytes> [mode]
//   roundtrip (default): upload, HEAD offset, download, SHA compare
//   terminate: upload, then abort(true) to DELETE, expect 404
//   empty:     upload a 0-byte file, expect offset 0 and empty download
//   metadata:  upload with metadata, verify HEAD echoes Upload-Metadata
//   smallchunk: upload with a tiny chunkSize (many PATCHes)
//   resume:    upload one chunk, abort, resume via uploadUrl, verify bytes
import crypto from "node:crypto";
import * as tus from "tus-js-client";

const [endpoint, sizeStr, mode = "roundtrip"] = process.argv.slice(2);
const SIZE = Number(sizeStr);
const data = mode === "empty" ? Buffer.alloc(0) : crypto.randomBytes(SIZE);
const sha = crypto.createHash("sha256").update(data).digest("hex");

const HEAD_HEADERS = { "Tus-Resumable": "1.0.0" };

function makeUpload(extra = {}) {
  return new Promise((resolve, reject) => {
    const upload = new tus.Upload(data, {
      endpoint,
      chunkSize: extra.chunkSize ?? 256 * 1024,
      retryDelays: [0, 100],
      metadata:
        extra.metadata ?? { filename: "interop.bin", filetype: "application/octet-stream" },
      onError: reject,
      onSuccess: () => resolve(upload),
    });
    upload.start();
  });
}

async function downloadSha(url) {
  const got = Buffer.from(await (await fetch(url)).arrayBuffer());
  return crypto.createHash("sha256").update(got).digest("hex");
}

if (mode === "terminate") {
  const upload = await makeUpload();
  await upload.abort(true);
  const head = await fetch(upload.url, { method: "HEAD", headers: HEAD_HEADERS });
  if (head.status !== 404) throw new Error(`expected 404 after terminate, got ${head.status}`);
  console.log("OK terminated " + upload.url);
} else if (mode === "empty") {
  const upload = await makeUpload();
  const head = await fetch(upload.url, { method: "HEAD", headers: HEAD_HEADERS });
  if (head.headers.get("upload-offset") !== "0")
    throw new Error(`empty offset: ${head.headers.get("upload-offset")}`);
  const got = Buffer.from(await (await fetch(upload.url)).arrayBuffer());
  if (got.length !== 0) throw new Error(`empty download not empty: ${got.length}`);
  console.log("OK empty " + upload.url);
} else if (mode === "metadata") {
  const upload = await makeUpload({ metadata: { filename: "wîndé.bin", filetype: "image/png" } });
  const head = await fetch(upload.url, { method: "HEAD", headers: HEAD_HEADERS });
  const meta = head.headers.get("upload-metadata") || "";
  const decoded = Object.fromEntries(
    meta.split(",").map((kv) => {
      const [k, v] = kv.trim().split(" ");
      return [k, v ? Buffer.from(v, "base64").toString("utf-8") : ""];
    }),
  );
  if (decoded.filename !== "wîndé.bin") throw new Error(`filename lost: ${decoded.filename}`);
  if (decoded.filetype !== "image/png") throw new Error(`filetype lost: ${decoded.filetype}`);
  console.log("OK metadata " + upload.url);
} else if (mode === "smallchunk") {
  const upload = await makeUpload({ chunkSize: 64 * 1024 });
  if ((await downloadSha(upload.url)) !== sha) throw new Error("smallchunk sha mismatch");
  console.log("OK smallchunk " + upload.url);
} else if (mode === "resume") {
  // Upload one chunk, abort, then resume against the same uploadUrl.
  const url = await new Promise((resolve, reject) => {
    const up = new tus.Upload(data, {
      endpoint,
      chunkSize: 256 * 1024,
      onChunkComplete: () => up.abort().then(() => resolve(up.url)),
      onError: reject,
    });
    up.start();
  });
  await new Promise((resolve, reject) => {
    const up = new tus.Upload(data, {
      uploadUrl: url,
      chunkSize: 256 * 1024,
      onSuccess: resolve,
      onError: reject,
    });
    up.start();
  });
  if ((await downloadSha(url)) !== sha) throw new Error("resume sha mismatch");
  console.log("OK resume " + url);
} else {
  const upload = await makeUpload();
  const head = await fetch(upload.url, { method: "HEAD", headers: HEAD_HEADERS });
  if (head.headers.get("upload-offset") !== String(SIZE))
    throw new Error(`bad offset: ${head.headers.get("upload-offset")}`);
  if ((await downloadSha(upload.url)) !== sha) throw new Error("sha mismatch on download");
  console.log("OK " + upload.url);
}
