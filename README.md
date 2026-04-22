# First MCP server: Oracle (read tools)

This project is a **Model Context Protocol (MCP) server** written in Python. It exposes **tools** that an MCP-aware client (for example **Cursor**) can call so that the assistant can fetch **read-only** data from an **Oracle** database and answer your questions with that context.

---

## What is MCP (in one minute)?

- **MCP** is a standard way for an application to give an AI **structured capabilities**: tools, resources, and prompts.
- Your **server** is a normal process that speaks MCP; often over **stdio** (standard input/output): the client spawns the process and exchanges JSON messages on pipes—no web server is required.
- The **user’s chat** is handled by the client and the model. The model decides **when** to call a **tool** (for example, run a SQL `SELECT`). The server runs the work and returns text (here, JSON) back to the model, which then replies to you in natural language.

So: the MCP server does **not** “listen to your chat” directly. It responds to **tool invocations** that the client sends when the assistant needs database results.

---

## Architecture (Mermaid)

The diagrams use **User** (you), **Cursor** (the IDE and its AI client), **MCP** (this Python server process), and **Oracle** (the database). Credentials for Oracle are supplied to the **MCP** process via environment variables (configured in Cursor’s MCP settings), not by sending passwords in chat.

**Preview in the editor:** Cursor and VS Code’s built-in **Markdown preview** does not render Mermaid by default; you usually only see a fenced `mermaid` code block. To see diagrams: install an extension such as [Markdown Preview Mermaid Support](https://marketplace.visualstudio.com/items?itemName=bierner.markdown-mermaid) (or similar), or open this file on **GitHub** in the browser, where Mermaid in READMEs is rendered automatically.

### Components and connections

```mermaid
flowchart LR
  User["User"]

  subgraph cursor_app["Cursor"]
    Cursor["Cursor IDE + AI assistant\n(MCP client)"]
  end

  subgraph local["This machine (typical)"]
    MCP["MCP server: oracle_mcp_server.py\nPython + FastMCP + oracledb"]
  end

  subgraph data["Database tier"]
    Oracle["Oracle database\n(tables, views, DB user)"]
  end

  User -->|chat| Cursor
  Cursor <-->|"stdio: MCP (JSON-RPC)"| MCP
  MCP -->|"SQL via connection pool"| Oracle
```

### What happens when you ask a question

```mermaid
sequenceDiagram
  autonumber
  actor User as User
  participant Cursor as Cursor<br/>(IDE + model + MCP client)
  participant MCP as MCP server<br/>(Python)
  participant Oracle as Oracle DB

  User->>Cursor: Chat: ask in natural language
  Note over Cursor: Model may call a tool<br/>query / list_tables / describe_table
  Cursor->>MCP: Tool invocation (name + arguments)
  MCP->>Oracle: Run SQL (read-only, row cap)
  Oracle-->>MCP: Result rows
  MCP-->>Cursor: Tool output (JSON text)
  Cursor-->>User: Assistant reply using results
```

### Where the database user fits

```mermaid
flowchart TB
  User["User"]
  Cursor["Cursor"]
  MCP["MCP server process"]
  Env["Env: ORACLE_USER, ORACLE_PASSWORD,\nORACLE_DSN, …\n(set in MCP config)"]
  Oracle["Oracle DB\nvalidates ORACLE_USER"]

  User -->|configures once| Cursor
  Cursor -->|starts process with env| Env
  Env --> MCP
  MCP -->|connects as DB user| Oracle
```

The **Oracle** side only sees the **database account** named in `ORACLE_USER` (and the password you set in the environment). That is separate from your **Cursor** login or chat history.

---

## What this server provides

| Tool | Purpose |
|------|--------|
| `query` | Run a **single** `SELECT` or `WITH` (CTE) query, optional named binds, max **200** rows. |
| `list_tables` | List tables in a schema (`ALL_TABLES`), optional `LIKE` on table name. |
| `describe_table` | Column list for a table (`ALL_TAB_COLUMNS`). |

Queries are **heuristically** restricted to read-only use (no `INSERT`/`UPDATE`/etc. in the checked string). You should still use a **least-privileged** database user; the check is not a full SQL security audit.

---

## Requirements

- **Python 3.10+**
- An Oracle database you can connect to, and a user with appropriate privileges to query the objects you need (`ALL_TABLES` / `ALL_TAB_COLUMNS` require the right visibility for those views).

**Driver:** this project uses [python-oracledb](https://python-oracledb.readthedocs.io/) (`oracledb` on PyPI).

- **Thin mode (default in this project):** no Oracle Instant Client; use an **Easy Connect** DSN: `host:port/service_name`.
- **Thick mode (optional):** for **TNS names** and `tnsnames.ora`, set `ORACLE_USE_THICK=1`, install **Oracle Instant Client**, set `TNS_ADMIN` to the directory containing `tnsnames.ora`, and set `ORACLE_DSN` to the **TNS alias**. See the environment table below.

---

## Setup

### 1. Create a virtual environment and install dependencies

```powershell
cd c:\PVMEHTA\Github\Pares11804\firstmcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Set environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ORACLE_USER` | Yes | Database username. |
| `ORACLE_PASSWORD` | Yes | Password for that user. |
| `ORACLE_DSN` | Yes | **Thin:** `hostname:port/service_name` (Easy Connect). **Thick:** TNS **alias** if using `tnsnames.ora`. |
| `ORACLE_USE_THICK` | No | Set to `1` / `true` / `yes` to use Instant Client and thick mode. |
| `TNS_ADMIN` | No* | *Directory* containing `tnsnames.ora` (required for TNS resolution with Net). Often set in the system environment, not only in the app. |
| `ORACLE_CLIENT_LIB_DIR` | No | Path to Instant Client for `oracledb.init_oracle_client()` if the client is not on `PATH`. |
| `ORACLE_POOL_MAX` | No | Max pool size (default `4`). |

Never commit real passwords. Prefer the client’s **MCP “env”** block or a local secret store, not a checked-in file.

### 3. Register the server in Cursor

In Cursor, add an MCP server that runs this script with your venv’s Python. Conceptually you need:

- **Command:** path to `python.exe` inside `.venv`
- **Arguments:** full path to `oracle_mcp_server.py`
- **Environment:** the `ORACLE_*` variables (and `TNS_ADMIN` if using TNS files)

The exact file for MCP settings depends on your Cursor version (user-level MCP config or project-level). After saving, **reload** MCP or restart Cursor so the new server appears.

### 4. Use it in chat

- Enable the Oracle MCP in your session.
- Ask in natural language, for example: “List tables in schema `MYSCHEMA`” or “What columns does `MYSCHEMA.MY_TABLE` have?”
- The assistant will call `list_tables` / `describe_table` or `query` with SQL and then summarize the JSON result.

If the model does not call the tool, say explicitly: “Use the Oracle MCP `query` tool to run: …”

---

## How the code is organized

- **`oracle_mcp_server.py`**  
  - **`FastMCP`**: defines the app name, registers tools, runs **stdio** transport.  
  - **`get_pool()`**: lazy singleton connection pool, thick vs thin per env.  
  - **`_is_read_only_select()`**: lightweight guard; not a full SQL parser.  
  - **`_run_read_query()`**: executes validated SQL, caps rows, returns JSON text.  
  - Tool functions: thin wrappers (docstrings are shown to the model as tool help).

- **`requirements.txt`**: `fastmcp` and `oracledb`.

---

## Security notes

- Use a **read-only** or **limited** user for day-to-day assistant access if possible.
- The `query` tool can still run any `SELECT` your user is allowed to run, including over broad views—plan grants accordingly.
- Do not **paste secrets** into chat. Configure credentials in **environment variables** the MCP process inherits.

---

## Troubleshooting

| Symptom | Things to check |
|--------|------------------|
| “Set ORACLE_USER…” | `ORACLE_USER` / `ORACLE_PASSWORD` / `ORACLE_DSN` missing in the **MCP** env, not just your shell. |
| DPI / Instant Client errors | For thick mode: Instant Client **bitness** matches Python; `TNS_ADMIN` points at the folder with `tnsnames.ora`; `ORACLE_DSN` is the **alias** name. |
| `ORA-12154` / TNS | TNS name not resolved: verify `TNS_ADMIN`, file name `tnsnames.ora`, and the alias in `ORACLE_DSN`. |
| Permission errors on `ALL_*` views | The DB user may need to be granted access or use `USER_TABLES` / `USER_TAB_COLUMNS` for its own objects (would require a small code change to those views). |

---

## Local sanity check (optional)

With env vars set in your shell:

```powershell
$env:ORACLE_USER = "…"
$env:ORACLE_PASSWORD = "…"
$env:ORACLE_DSN = "host:1521/orcl"   # example thin DSN
python oracle_mcp_server.py
```

If the process starts and blocks on stdio, that is expected: **Cursor** is the client that will attach to it. For a quick **DB** test, use a one-off script with `oracledb.connect` and `SELECT 1 FROM DUAL` before relying on MCP.

---

## License

Add a license if you share this repository; none is set by this README by default.
