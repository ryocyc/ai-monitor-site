# Cloudflare Worker: send-push

Free-tier MVP push backend for medication-reminder-app.

## What It Does

Receives push token + message from the app, forwards to Expo Push API.

## Why Cloudflare Workers

- **Free tier**: 100,000 requests/day, no credit card required
- **Global edge**: Runs in 300+ data centers, low latency
- **No server setup**: Deploy in seconds with `wrangler`
- **MVP scope**: Perfect for a simple relay between app and Expo Push

## Deploy Steps

### 1. Install Wrangler CLI

```bash
npm install -g wrangler
```

### 2. Login to Cloudflare

```bash
wrangler login
```

Opens browser for GitHub authentication (free account).

### 3. Configure Account ID (for deploy)

Get your Account ID from Cloudflare Dashboard > Overview > Account ID,
or from `.wrangler/config/config.json` after `wrangler login`.

Edit `wrangler.toml` and set:
```toml
account_id = "YOUR_32_CHAR_ACCOUNT_ID"
```

### 4. Deploy

```bash
cd worker
wrangler deploy
```

Copy the `.cloudflareworkers.com` URL printed at the end (e.g.
`https://send-push.YOUR_SUBDOMAIN.workers.dev`).

### 5. Update Client

In `src/pushService.ts`, set:
```ts
const PUSH_WORKER_URL = 'https://send-push.YOUR_SUBDOMAIN.workers.dev';
```

## Free Tier Limits

| Limit | Value |
|-------|-------|
| Requests/day | 100,000 |
| CPU time/request | 10ms |
| Bandwidth | Free |

Sending to Expo Push is a single outbound HTTP request — well within limits.

## Production Hardening (Not MVP Scope)

Before production, add at minimum:

1. **Authentication**: Uncomment the `x-push-secret` header check in `src/index.ts`.
   Set `PUSH_SECRET` in `wrangler.toml` or Cloudflare dashboard.
2. **Rate limiting**: Cloudflare dashboard > Workers > send-push > Settings > Resources >
   Rate Limiting (free tier: 100 req/min)
3. **Bot protection**: Cloudflare dashboard > Security > WAF > Bot Fight Mode
4. **Private Worker**: Restrict to your app's domain via Cloudflare Access
   (requires Cloudflare Zero Trust, paid tier — or keep open with secret)

## Request/Response Examples

### Send push

```bash
curl -X POST https://send-push.YOUR_SUBDOMAIN.workers.dev \
  -H "Content-Type: application/json" \
  -d '{
    "tokens": ["ExponentPushToken[xxxxx]"],
    "title": "Medication Reminder",
    "body": "Time to take Metformin 500mg",
    "memberIds": ["member-1"]
  }'
```

### Response

```json
{
  "success": true,
  "sent": 1,
  "failed": 0,
  "results": [{ "ok": true }],
  "memberIds": ["member-1"]
}
``` 