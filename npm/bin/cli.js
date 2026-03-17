#!/usr/bin/env node
/**
 * Adoback — npm CLI 入口
 *
 * 这个文件只是一个路由器：
 *   - 如果有预编译二进制 → 直接执行
 *   - 如果是源码模式 → 通过 Python 执行
 *   - 都没有 → 提示重新安装
 */

"use strict";

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const BIN_DIR = __dirname;
const BINARY_PATH = path.join(BIN_DIR, "adoback-bin");
const FALLBACK_PATH = path.join(BIN_DIR, "..", "fallback", "adoback.py");
const MARKER = path.join(BIN_DIR, ".mode");

const args = process.argv.slice(2);

function run(cmd, cmdArgs) {
  try {
    execFileSync(cmd, cmdArgs, { stdio: "inherit" });
  } catch (e) {
    process.exit(e.status || 1);
  }
}

// 读取安装模式
let mode = "";
try {
  mode = fs.readFileSync(MARKER, "utf-8").trim();
} catch {}

if (mode === "unsupported") {
  console.error("\u2717 adoback \u4EC5\u652F\u6301 macOS");
  process.exit(1);
}

if (mode === "binary" && fs.existsSync(BINARY_PATH)) {
  run(BINARY_PATH, args);
} else if (mode.startsWith("source:") && fs.existsSync(FALLBACK_PATH)) {
  const python = mode.split(":")[1];
  run(python, [FALLBACK_PATH, ...args]);
} else {
  console.error("\u2717 adoback \u672A\u6B63\u786E\u5B89\u88C5");
  console.error("  \u8BF7\u91CD\u65B0\u5B89\u88C5: npm install -g adoback");
  process.exit(1);
}
