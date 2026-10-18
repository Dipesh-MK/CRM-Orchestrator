# Aggregator Integration & Contract Reference

This document provides the complete technical integration specification for the Mock Aggregator (`./Mock Aggregator`) running as an independent SMS gateway on port 3000. It documents all routes, authentication mechanisms, status progressions, callback systems, registered handset models, and architectural rationale for the CRM Orchestrator client integration.

---

## 1. Complete Mock Aggregator Endpoint Catalog

| Endpoint Path | HTTP Method | Auth Scheme | Request Payload | Response Schema | Side Effects |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `/2010-04-01/Accounts/:accountSid/Messages.json` | `POST` | HTTP Basic (`AccountSid:AuthToken` or `ApiKey:ApiSecret`) | Form-urlencoded or JSON: `{ "To": "+1...", "From": "55512", "Body": "...", "StatusCallback": "http://..." }` | Twilio 201 Created: `{ "sid": "SM...", "status": "queued", "to": "...", "from": "...", "body": "...", "price": "-0.0075", ... }` | Inserts record into `messages` table with status `queued`; broadcasts `message:new` over WebSocket; schedules async delivery simulation via `scheduleDelivery(sid)`. |
| `/2010-04-01/Accounts/:accountSid/Messages/:sid.json` | `GET` | HTTP Basic (`AccountSid:AuthToken` or `ApiKey:ApiSecret`) | None | Twilio Message Snapshot JSON (returns stored `raw_response_body`) | None (read-only query). |
| `/api/client/send-sms` | `POST` | Cookie/Bearer Session Auth (`requireAuth`) | JSON: `{ "from": "55512", "to": "+1...", "body": "...", "status_callback_url": "..." }` | JSON Envelope: `{ "status": "success", "data": { "sid": "SM...", "status": "queued", ... } }` | Inserts message into `messages`; broadcasts `message:new`; schedules `scheduleDelivery(sid)`. |
| `/api/client/messages` | `GET` | Cookie/Bearer Session Auth (`requireAuth`) | Query params: `status`, `search`, `limit`, `offset` | JSON Envelope: `{ "status": "success", "data": [ ... ], "meta": { "pagination": ... } }` | None (reads tenant messages). |
| `/api/client/messages/:sid` | `GET` | Cookie/Bearer Session Auth (`requireAuth`) | None | JSON Envelope: `{ "status": "success", "data": { ...msg, raw_request_headers, raw_request_body, webhook_deliveries } }` | None (wire inspection). |
| `/api/client/account` | `GET` | Cookie/Bearer Session Auth (`requireAuth`) | None | JSON Envelope: `{ "status": "success", "data": { "account": { ... } } }` | None. |
| `/api/client/users` | `GET` / `POST` / `DELETE` | Cookie/Bearer Session Auth (`requireTenantAdmin` on write) | User JSON | JSON Envelope | Modifies `account_users` table. |
| `/api/client/webhooks` | `PATCH` | Cookie/Bearer Session Auth (`requireTenantAdmin`) | JSON: `{ "status_callback_url": "...", "inbound_callback_url": "..." }` | JSON Envelope: `{ "status": "success", "data": account }` | Updates tenant URLs in `accounts` table. |
| `/api/admin/accounts` | `GET` | None (Internal Admin) | None | JSON Envelope: `[ { "id": "...", "name": "...", "shortcodes": [...], "stats": {...} } ]` | None. |
| `/api/admin/accounts/:id` | `GET` | None (Internal Admin) | None | JSON Envelope: `{ account, shortcodes, users, api_keys }` | None. |
| `/api/admin/accounts/:id/webhooks` | `PATCH` | None (Internal Admin) | JSON: `{ status_callback_url, inbound_callback_url }` | JSON Envelope | Updates `accounts` record. |
| `/api/admin/messages` | `GET` | None (Internal Admin) | Query params: `account_id`, `status`, `limit`, `offset` | JSON Envelope: `{ "status": "success", "data": [...], "meta": { "pagination": ... } }` | None. |
| `/api/admin/messages/:sid` | `GET` | None (Internal Admin) | None | JSON Envelope: `{ ...msg, raw_request_headers, raw_request_body, raw_response_body, webhook_deliveries }` | None. |
| `/api/admin/stats` | `GET` | None (Internal Admin) | None | JSON Envelope: `{ total_messages, delivered, undelivered, queued, sent, inbound, ... }` | None. |
| `/api/admin/sim/settings` | `GET` / `POST` | None (Internal Admin) | POST JSON: `{ "sent_delay_ms": 500, "dlr_delay_ms": 1500 }` | JSON Envelope: updated settings | Updates `simulation_settings` table. |
| `/api/phone/numbers` | `GET` | None | None | JSON Envelope: `{ "status": "success", "data": [ { "phone_number": "+15551234561", "display_name": "...", "simulated_behavior": "DEFAULT" } ] }` | None. |
| `/api/phone/register` | `POST` | None | JSON: `{ "phone_number": "+1...", "display_name": "..." }` | JSON Envelope: registered virtual phone record | Inserts into `virtual_phones` table (`INSERT OR IGNORE`). |
| `/api/phone/:phoneNumber/threads` | `GET` | None | None | JSON Envelope: threads grouped by shortcode with full message history | None. |
| `/api/phone/reply` | `POST` | None | JSON: `{ "from": "+15551234561", "to": "55512", "body": "STOP" }` | JSON Envelope: `{ "status": "success", "data": { "sid": "SM...", ... } }` | Inserts inbound message into `messages`; auto-registers phone; broadcasts `message:inbound`; dispatches inbound webhook. |
| `/health` | `GET` | None | None | JSON: `{ "status": "ok", "uptime": 123.4, "timestamp": "..." }` | None. |

---

## 2. Message Status Progression & Deterministic Simulator

### A. Lifecycle State Machine
```
[Client POST]
     │
     ▼
  queued  ───────────────►  sent  ───────────────►  delivered (terminal)
(at ingestion)       (after sent_delay_ms)   OR   (digits 1-9)
                                                  undelivered (terminal)
                                                  (digit 0, Code 30008)
```

1. **`queued`**: Persisted immediately upon receiving valid request. Returned in the HTTP 201 response payload.
2. **`sent`**: Transitioned automatically by `scheduleDelivery` timer after `sent_delay_ms` (default 500ms). Updates `sent_at` timestamp. Emits WebSocket event `message:status` (`status: "sent"`).
3. **`delivered` / `undelivered`**: Transitioned automatically after an additional `dlrDelayMs` (default 1500ms):
   - **Deterministic Phone Number Suffix Rule**:
     - Destination numbers ending in `'0'` transition to `undelivered` with `error_code: "30008"` ("Simulated aggregator delivery failure: carrier rejected message").
     - Destination numbers ending in `'1'` through `'9'` transition to `delivered` with `delivered_at` timestamp populated.
   - Emits WebSocket event `message:status` with terminal status.
   - Triggers `dispatchStatusCallback`.

---

## 3. Webhook Delivery & Callback Mechanism

### A. Dispatch Trigger
When a message reaches a terminal status (`delivered` or `undelivered`), the simulator checks:
1. Per-message `status_callback_url` provided in the send request.
2. If omitted, falls back to the account-level `status_callback_url` registered in `accounts`.
3. If neither is set, webhook delivery is skipped.

### B. Payload Format
The webhook is dispatched as an `application/x-www-form-urlencoded` HTTP POST request matching Twilio's standard webhook specification:
```
MessageSid=SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AccountSid=AC_IDACT_001
From=55512
To=%2B15551234561
MessageStatus=delivered   (or 'undelivered')
SmsStatus=delivered       (or 'undelivered')
ApiVersion=2010-04-01
ErrorCode=30008           (included if undelivered)
ErrorMessage=...          (included if undelivered)
```

### C. Webhook Telemetry
Each dispatch attempt records an entry in `webhook_deliveries`:
- `id`: Unique webhook delivery ID.
- `message_sid`: Target message SID.
- `webhook_type`: `STATUS_CALLBACK`.
- `url`, `http_method`: Target URL and POST method.
- `payload`: Form-encoded payload.
- `response_status_code`, `response_body`, `latency_ms`: Captured upstream response telemetry.

---

## 4. Handsets & Virtual Phone Registration Model

### A. Data Representation
Virtual handsets represent simulated subscriber phones and are stored in `virtual_phones`:
- `phone_number`: Unique phone identifier in E.164 format (e.g. `+15551234561`).
- `display_name`: Human-friendly label (e.g. `Alice Johnson (Pixel 8)`).
- `simulated_behavior`: Behavioral flag (`DEFAULT`, `ALWAYS_FAIL`, `DELAYED`).

### B. Creation & Linking
- Created via `POST /api/phone/register` or automatically created upon receiving an inbound reply via `POST /api/phone/reply`.
- Handset views, conversation threads, and simulated replies are accessible via `/phone.html` or `/api/phone/:phoneNumber/threads`.
- **Querying Registered Handsets**: `GET /api/phone/numbers` returns the complete list of virtual handsets currently configured in the aggregator website.

---

## 5. Selection of the Best CRM Send Endpoint

### Evaluated Alternatives
1. `/api/client/send-sms`:
   - Requires browser session cookies or JWT login to an account user.
   - Not standard across enterprise aggregators; designed for the internal portal UI.
2. `/api/v1/messages`:
   - Does not exist in the mock codebase.
3. `/2010-04-01/Accounts/:accountSid/Messages.json`:
   - **SELECTED BEST ENDPOINT**.

### Technical Justification
1. **Industry-Standard Parity**: Implements the de facto Twilio REST standard used by telecom aggregators worldwide.
2. **Clean Server-to-Server Authentication**: Uses standard HTTP Basic Auth (`AccountSid:AuthToken`), cleanly isolated in environment variables without requiring UI session tokens or interactive logins.
3. **Structured Error Handling**: Returns standard HTTP 400/401 with Twilio error codes (e.g. `21602` for missing parameters, `21606` for invalid short code, `20003` for auth failure).
4. **Tool Contract Alignment**: Exactly matches `generated/aggregator/tools.spec.yaml` and the human-approved G2/G5 contract `post_2010_04_01_accounts_account_sid_messages_json`.
