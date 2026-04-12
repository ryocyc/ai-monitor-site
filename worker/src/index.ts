/**
 * Cloudflare Worker: send-push
 *
 * Free-tier MVP push backend for medication-reminder-app.
 * Receives push tokens + message from client, forwards to Expo Push API.
 *
 * DEPLOYMENT:
 *   1. npm install -g wrangler
 *   2. wrangler login (GitHub account, free tier is fine)
 *   3. wrangler deploy
 *   4. Copy the .cloudflareworkers.com URL printed after deploy
 *   5. Paste it as PUSH_WORKER_URL in src/pushService.ts
 *
 * FREE TIER LIMITS:
 *   100,000 requests/day
 *   10ms CPU time/request (push send is fast, well under limit)
 *   No outbound bandwidth charges for Expo Push API calls
 *
 * REQUEST SHAPE (POST JSON):
 *   {
 *     tokens: string[],        // Expo push tokens
 *     title: string,           // Notification title
 *     body: string,            // Notification body
 *     memberIds?: string[]    // Optional: member IDs for logging (not used for send)
 *   }
 *
 * RESPONSE SHAPE:
 *   {
 *     success: boolean,
 *     sent: number,
 *     failed: number,
 *     results: Array<{ ok: boolean, error?: string }>
 *   }
 *
 * SECURITY (MVP NOTE):
 *   This Worker has no built-in auth. For production, add:
 *   - CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET header validation
 *   - Or a simple shared secret header check
 *   - Cloudflare Turnstile for bot protection
 *   - Rate limiting via Cloudflare dashboard (100 req/min is default free)
 *
 *   DO NOT expose this Worker publicly without auth in production.
 */

const EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send';

interface Env {
  // Optional: set a secret in Cloudflare dashboard > Workers > send-push > Settings > Variables
  // Then uncomment the header check below
  // PUSH_SECRET?: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    if (request.method !== 'POST') {
      return new Response(JSON.stringify({ error: 'Method not allowed' }), {
        status: 405,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }

    // Optional: shared secret auth (uncomment and set PUSH_SECRET in wrangler.toml or dashboard)
    // const secret = env.PUSH_SECRET;
    // if (secret) {
    //   const authHeader = request.headers.get('x-push-secret');
    //   if (authHeader !== secret) {
    //     return new Response(JSON.stringify({ error: 'Unauthorized' }), {
    //       status: 401,
    //       headers: { ...corsHeaders, 'Content-Type': 'application/json' },
    //     });
    //   }
    // }

    let body: { tokens?: string[]; title?: string; body?: string; memberIds?: string[] };
    try {
      body = await request.json();
    } catch {
      return new Response(JSON.stringify({ error: 'Invalid JSON body' }), {
        status: 400,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }

    const { tokens, title, body: pushBody, memberIds } = body;

    if (!tokens || !Array.isArray(tokens) || tokens.length === 0) {
      return new Response(JSON.stringify({ error: 'tokens must be a non-empty array' }), {
        status: 400,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }
    if (!title || !pushBody) {
      return new Response(JSON.stringify({ error: 'title and body are required' }), {
        status: 400,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }

    const results: { ok: boolean; error?: string }[] = [];
    let sent = 0;
    let failed = 0;

    await Promise.all(
      tokens.map(async (token, index) => {
        try {
          const expoPayload = {
            to: token,
            title,
            body: pushBody,
            sound: 'default',
          };

          const response = await fetch(EXPO_PUSH_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(expoPayload),
          });

          if (response.ok) {
            results[index] = { ok: true };
            sent++;
          } else {
            const errorText = await response.text();
            results[index] = { ok: false, error: `Expo ${response.status}: ${errorText.slice(0, 100)}` };
            failed++;
          }
        } catch (err) {
          results[index] = { ok: false, error: String(err) };
          failed++;
        }
      })
    );

    const responseBody = {
      success: sent > 0,
      sent,
      failed,
      results,
      // Echo back memberIds if provided (for client-side logging/debugging)
      ...(memberIds ? { memberIds } : {}),
    };

    return new Response(JSON.stringify(responseBody), {
      status: 200,
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
    });
  },
};