#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { DefaultArtifactClient } = require("@actions/artifact");

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    throw new Error(`missing ${name}`);
  }
  return process.argv[index + 1];
}

function filesBelow(directory) {
  const result = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const child = path.join(directory, entry.name);
    if (entry.isDirectory()) result.push(...filesBelow(child));
    else if (entry.isFile()) result.push(child);
  }
  return result;
}

async function main() {
  const name = argument("--name");
  const root = path.resolve(argument("--root"));
  const checkpoint = path.resolve(argument("--path"));
  if (!/^sarguardian-live-failure-[0-9]+-[0-9]+-(?:GUNW|GOFF)-[0-9]{3,5}$/.test(name)) {
    throw new Error("invalid artifact name");
  }
  if (checkpoint !== root && !checkpoint.startsWith(root + path.sep)) {
    throw new Error("checkpoint is outside root");
  }
  if (!fs.statSync(checkpoint).isDirectory()) {
    throw new Error("checkpoint directory is missing");
  }
  const files = filesBelow(checkpoint);
  if (files.length === 0) throw new Error("checkpoint contains no files");
  const client = new DefaultArtifactClient();
  const response = await client.uploadArtifact(name, files, root, {
    retentionDays: 14,
    compressionLevel: 6,
  });
  process.stdout.write(
    `LIVE_FAILURE_ARTIFACT_UPLOAD=PASS name=${name} files=${files.length} id=${response.id}\n`,
  );
}

main().catch((error) => {
  process.stderr.write(`ERROR=LIVE_FAILURE_ARTIFACT_UPLOAD_ERROR detail=${String(error.message || error)}\n`);
  process.exitCode = 1;
});
