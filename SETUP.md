# ServiceNow MCP on Render

The complete server.py preserves all 10 tools and uses the official Python MCP SDK 1.26.0. Install the included requirements together with this file. Your original server has not been overwritten.

## 1. Upload the files to GitHub

Create a private repository named servicenow-mcp-render. Upload server.py, requirements.txt, .python-version and .gitignore to its top level. Do not upload your .env or .venv. Check that .python-version contains the single line 3.12; if your browser omits dotfiles, create it using Add file > Create new file.

## 2. Generate a private token

On the computer where your Python installation works, run:

```console
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Keep the result privately. Use the same value as MCP_API_TOKEN in Render and as the Bearer Token in NetBrain. It is separate from your ServiceNow password. Never commit it to GitHub.

## 3. Create the Render service

Open https://dashboard.render.com/ and choose New > Web Service. Connect GitHub and select your repository.

| Setting | Value |
| --- | --- |
| Name | servicenow-mcp or an available name |
| Language | Python 3 |
| Branch | Your uploaded branch, usually main |
| Root Directory | Blank if files are at the top level |
| Build Command | pip install -r requirements.txt |
| Start Command | python server.py |
| Instance Type | Free |
| Health Check Path | /health |

Add these Environment variables before deploying:

| Variable | Value |
| --- | --- |
| SN_INSTANCE | Full base URL, such as https://dev123456.service-now.com |
| SN_USERNAME | Your existing ServiceNow API username |
| SN_PASSWORD | Your existing ServiceNow API password |
| MCP_API_TOKEN | The random token you generated |

Enter values without surrounding quotes. SN_INSTANCE must not include /login.do or an API path. No .env upload is required. Render supplies PORT and RENDER_EXTERNAL_HOSTNAME automatically. The server listens on 0.0.0.0 at PORT, and Render provides HTTPS. The .python-version file selects Python 3.12.

Click Create/Deploy Web Service and watch Logs until it is live. No database is needed.

## 4. Check the deployment

Copy the exact public URL from Render. Open https://YOUR-SERVICE.onrender.com/health and expect {"status":"ok"}. This checks the app, not your ServiceNow credentials.

The MCP endpoint is https://YOUR-SERVICE.onrender.com/mcp. Do not add a port number. Opening it in a browser without a token should return 401 Unauthorized; it is not a web page.

## 5. Configure NetBrain

In Add API Authenticator, choose:

- Name: ServiceNow MCP Render
- Method: Bearer Token
- Token/value: the raw MCP_API_TOKEN from Render
- Authentication Failure Condition: $Status Code Equals 401

The outgoing header must be Authorization: Bearer YOUR_TOKEN with exactly one Bearer prefix. The Bearer Token form normally adds the prefix. If needed, the API Key option shown in your screenshot can instead send Key = Authorization, Value = Bearer YOUR_TOKEN, Add to = Header. Do not put the token in the URL. Save the authenticator.

Then configure the MCP server:

| Setting | Value |
| --- | --- |
| Server Name | ServiceNow MCP |
| Server URL | https://YOUR-SERVICE.onrender.com/mcp |
| Connection Type | Streamable HTTP |
| Authenticator | ServiceNow MCP Render |

This is shared-token authentication for your POC, not an OAuth login flow. ServiceNow credentials stay in Render.

Connect and discover the 10 tools: test_servicenow_connection, get_incident, create_incident, update_incident, close_incident, get_change, create_change, update_change, search_changes, close_change.

First run test_servicenow_connection without arguments. Then run get_incident with an existing incident_number. Test writes on a disposable test record after reads succeed.

## Troubleshooting

- Initial timeout: Render Free sleeps after 15 idle minutes and can take about one minute to wake. Open /health, wait for its JSON response, then retry NetBrain.
- MCP 401: check that the authenticator is selected, the tokens match, and the header has exactly one Bearer prefix.
- ServiceNow HTTP 401/403 in a tool result: check the upstream account permissions and credentials. Confirm the PDI is awake.
- 421 Invalid Host header: use the exact Render hostname. A custom domain must first be added to allowed_hosts and allowed_origins in the server.
- 403 Invalid Origin: check the log and allow only the specific trusted origin needed by the client.
- Startup failure: verify all four environment variables, the start command, and the complete uploaded files. MCP_API_TOKEN must be at least 32 ASCII characters without whitespace.
- Import error: use this server with the supplied requirements; its FastMCP import matches the pinned SDK.

## Checks completed

All original functions were compared and preserved. Local tests passed for health, rejecting missing/incorrect tokens, MCP initialization, discovery of all 10 tools, host/origin protection, and a simulated ServiceNow tool call. Live Render and ServiceNow integration still need to be tested.

## Sources

- https://render.com/docs/web-services
- https://render.com/docs/free
- https://render.com/docs/environment-variables
- https://render.com/docs/python-version
- https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0
