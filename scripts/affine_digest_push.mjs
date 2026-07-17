#!/usr/bin/env node
/**
 * Пушит ежемесячные дайджесты проектов (connector/out/digest_payload.json,
 * пишет connector.py --digest) в AFFiNE как markdown-документы.
 *
 * Ничего не знает про Bitrix24/NotebookLM — читает уже готовый JSON. Пишет в
 * AFFiNE не напрямую (это недокументированный Socket.IO/Yjs-протокол), а
 * поднимая уже проверенный на практике affine-mcp-server как короткоживущий
 * HTTP-процесс на loopback и вызывая его MCP-тулы (find_doc_by_title,
 * create_doc_from_markdown, replace_doc_with_markdown) — те же самые тулы,
 * что уже работают для существующего Obsidian→AFFiNE синка.
 *
 * Требуемые env: AFFINE_BASE_URL, AFFINE_API_TOKEN, AFFINE_WORKSPACE_ID.
 * affine-mcp-server должен быть установлен глобально (npm install -g
 * affine-mcp-server@<пиненная версия>) заранее — этот скрипт только его
 * запускает и останавливает.
 */
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import crypto from "node:crypto";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PAYLOAD_PATH = path.join(__dirname, "..", "out", "digest_payload.json");
const PORT = 3917;
const HTTP_TOKEN = crypto.randomBytes(24).toString("hex"); // только для этого процесса

function requireEnv(name) {
  const v = process.env[name];
  if (!v) {
    console.error(`❌ Нет обязательной переменной окружения ${name}`);
    process.exit(1);
  }
  return v;
}

async function waitForHealthz(baseUrl, timeoutMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const r = await fetch(`${baseUrl}/healthz`);
      if (r.ok) return;
    } catch {
      // ещё не поднялся — подождать и повторить
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`affine-mcp-server не ответил на /healthz за ${timeoutMs}мс`);
}

async function main() {
  const raw = await readFile(PAYLOAD_PATH, "utf-8").catch(() => "[]");
  const digests = JSON.parse(raw);
  if (!Array.isArray(digests) || digests.length === 0) {
    console.log("Нет дайджестов для пуша (пустой digest_payload.json) — выхожу.");
    return;
  }

  const AFFINE_BASE_URL = requireEnv("AFFINE_BASE_URL");
  const AFFINE_API_TOKEN = requireEnv("AFFINE_API_TOKEN");
  const AFFINE_WORKSPACE_ID = requireEnv("AFFINE_WORKSPACE_ID");

  console.log(`→ Запускаю affine-mcp-server (HTTP, порт ${PORT})...`);
  const child = spawn("affine-mcp", [], {
    env: {
      ...process.env,
      MCP_TRANSPORT: "http",
      PORT: String(PORT),
      AFFINE_BASE_URL,
      AFFINE_MCP_AUTH_MODE: "bearer",
      AFFINE_API_TOKEN,
      AFFINE_WORKSPACE_ID,
      AFFINE_MCP_HTTP_TOKEN: HTTP_TOKEN,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (d) => process.stdout.write(`  [affine-mcp] ${d}`));
  child.stderr.on("data", (d) => process.stderr.write(`  [affine-mcp] ${d}`));

  let exitCode = 0;
  try {
    const base = `http://127.0.0.1:${PORT}`;
    await waitForHealthz(base);

    const transport = new StreamableHTTPClientTransport(new URL(`${base}/mcp`), {
      requestInit: { headers: { Authorization: `Bearer ${HTTP_TOKEN}` } },
    });
    const client = new Client({ name: "b24-digest-push", version: "1.0.0" });
    await client.connect(transport);

    for (const d of digests) {
      const title = `Дайджест проекта — ${d.notebook_name} — ${d.month}`;
      try {
        const found = await client.callTool({
          name: "find_doc_by_title",
          arguments: { title },
        });
        const matches = JSON.parse(found.content?.[0]?.text ?? "{}").matches ?? [];

        if (matches.length > 0) {
          const docId = matches[0].id;
          await client.callTool({
            name: "replace_doc_with_markdown",
            arguments: { docId, markdown: d.markdown },
          });
          console.log(`  ✅ [${title}] обновлён (docId=${docId})`);
        } else {
          const created = await client.callTool({
            name: "create_doc_from_markdown",
            arguments: { title, markdown: d.markdown },
          });
          console.log(`  ✅ [${title}] создан: ${created.content?.[0]?.text ?? ""}`);
        }
      } catch (e) {
        console.error(`  ❌ [${title}] не удалось запушить в AFFiNE: ${e}`);
        exitCode = 1;
      }
    }

    await client.close();
  } finally {
    child.kill("SIGTERM");
  }

  process.exit(exitCode);
}

main().catch((e) => {
  console.error("❌ affine_digest_push.mjs упал:", e);
  process.exit(1);
});
