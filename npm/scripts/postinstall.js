#!/usr/bin/env node
/**
 * Adoback — npm postinstall
 *
 * 安装时自动下载 macOS 预编译二进制。
 * 如果下载失败，回退到 Python 源码模式。
 */

"use strict";

const { execSync, execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");
const https = require("https");

// ─── 配置 ───
const REPO = "SOULRAi/adoback"; // ← 改成你的 GitHub 仓库
const BIN_DIR = path.join(__dirname, "..", "bin");
const BINARY_PATH = path.join(BIN_DIR, "adoback-bin");
const FALLBACK_PATH = path.join(__dirname, "..", "fallback", "adoback.py");
const MARKER = path.join(BIN_DIR, ".mode"); // "binary" or "source"

// ─── 平台检查 ───
if (os.platform() !== "darwin") {
  console.log("\u26A0 adoback \u4EC5\u652F\u6301 macOS\uFF0C\u8DF3\u8FC7\u5B89\u88C5");
  fs.writeFileSync(MARKER, "unsupported");
  process.exit(0);
}

const arch = os.arch(); // "arm64" or "x64"
const assetName =
  arch === "arm64"
    ? "adoback-macos-arm64"
    : "adoback-macos-x86_64";

// ─── 工具函数 ───
function httpsGet(url) {
  return new Promise((resolve, reject) => {
    const get = (u) => {
      https
        .get(u, { headers: { "User-Agent": "adoback-npm" } }, (res) => {
          if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
            return get(res.headers.location);
          }
          if (res.statusCode !== 200) {
            return reject(new Error(`HTTP ${res.statusCode}`));
          }
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () => resolve(Buffer.concat(chunks)));
          res.on("error", reject);
        })
        .on("error", reject);
    };
    get(url);
  });
}

async function downloadBinary() {
  const apiUrl = `https://api.github.com/repos/${REPO}/releases/latest`;
  const meta = JSON.parse((await httpsGet(apiUrl)).toString());

  const asset = (meta.assets || []).find((a) =>
    a.name.toLowerCase().includes(assetName.toLowerCase())
  );
  if (!asset) return false;

  console.log(`  \u4E0B\u8F7D: ${asset.name} ...`);
  const bin = await httpsGet(asset.browser_download_url);
  fs.writeFileSync(BINARY_PATH, bin);
  fs.chmodSync(BINARY_PATH, 0o755);

  try {
    execFileSync(BINARY_PATH, ["--version"], { stdio: "pipe" });
  } catch {
    fs.unlinkSync(BINARY_PATH);
    return false;
  }

  return true;
}

function downloadSource() {
  const url = `https://raw.githubusercontent.com/${REPO}/main/adoback.py`;
  console.log("  \u4E0B\u8F7D\u6E90\u7801...");
  try {
    execSync(`curl -fsSL "${url}" -o "${FALLBACK_PATH}"`, { stdio: "pipe" });
    return fs.existsSync(FALLBACK_PATH);
  } catch {
    return false;
  }
}

function findPython() {
  for (const cmd of ["python3", "python"]) {
    try {
      const ver = execSync(
        `${cmd} -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')"`,
        { stdio: "pipe", encoding: "utf-8" }
      ).trim();
      const [major, minor] = ver.split(".").map(Number);
      if (major >= 3 && minor >= 10) return cmd;
    } catch {}
  }
  return null;
}

// ─── 主流程 ───
(async () => {
  console.log("");
  console.log("  Adoback \u00B7 \u5B89\u88C5");
  console.log("");

  try {
    if (await downloadBinary()) {
      fs.writeFileSync(MARKER, "binary");
      console.log("  \u2713 \u5DF2\u5B89\u88C5\u9884\u7F16\u8BD1\u4E8C\u8FDB\u5236");
      return;
    }
  } catch (e) {
    // 继续回退
  }

  console.log("  \u672A\u627E\u5230\u9884\u7F16\u8BD1\u4E8C\u8FDB\u5236\uFF0C\u5C1D\u8BD5\u6E90\u7801\u6A21\u5F0F...");

  const python = findPython();
  if (!python) {
    console.error("  \u2717 \u672A\u627E\u5230 Python 3.10+");
    console.error("    \u5B89\u88C5\u65B9\u5F0F: brew install python");
    console.error("    \u6216\u8005\u4ECE GitHub Releases \u624B\u52A8\u4E0B\u8F7D\u4E8C\u8FDB\u5236");
    process.exit(1);
  }

  if (downloadSource()) {
    fs.writeFileSync(MARKER, `source:${python}`);
    console.log(`  \u2713 \u5DF2\u5B89\u88C5\u6E90\u7801\u6A21\u5F0F (${python})`);
  } else {
    console.error("  \u2717 \u4E0B\u8F7D\u6E90\u7801\u5931\u8D25");
    process.exit(1);
  }
})();
