// Push service - Cloudflare Worker backend
//
// MVP SCOPE: This module provides token storage (Firestore REST) and real push sending
// via a Cloudflare Worker.
//
// FREE TIER: Cloudflare Workers free tier allows 100,000 requests/day — more than
// enough for a personal medication reminder MVP.
//
// CONTROLLED TESTING: Token writes to Firestore are enabled (FIREBASE_ENABLED = true).
// SERVER-SIDE PUSH: Real push sending via Worker is enabled (PUSH_WORKER_ENABLED = true).
//
// FIREBASE STATUS: Firestore is used only for client-side token persistence.
// No Firebase Functions are used for push sending.
// Firestore rules should still be set to allow writes to devices/ and familyMembers/.
//
// PRODUCTION REQUIREMENTS (not yet implemented):
//   - Shared secret header auth on the Worker
//   - Cloudflare rate limiting (free tier: 100 req/min)
//   - Bot protection via Cloudflare WAF
//   - Delivery receipts and status tracking

const FIREBASE_ENABLED = true;

const FIREBASE_PROJECT_ID = 'medication-reminder-mvp' as string;
const FIREBASE_API_KEY = 'AIzaSyA02_8Ij3UvtF7Fu69wX_veLXhuCN8Z_Sw' as string;

const FIRESTORE_BASE = `https://firestore.googleapis.com/v1/projects/${FIREBASE_PROJECT_ID}/databases/(default)/documents`;

// Cloudflare Worker URL - set after deploying with `wrangler deploy` from worker/ directory
// Replace with the actual .cloudflareworkers.com URL printed after deploy.
const PUSH_WORKER_URL = 'https://send-push.medreminder-ryo.workers.dev' as string;
const PUSH_WORKER_ENABLED = true;

export function isFirebaseReady(): boolean {
  if (!FIREBASE_ENABLED) return false;
  const placeholderProject = 'YOUR_FIREBASE_PROJECT_ID';
  const placeholderKey = 'YOUR_FIREBASE_API_KEY';
  return (
    FIREBASE_PROJECT_ID !== placeholderProject &&
    FIREBASE_API_KEY !== placeholderKey &&
    FIREBASE_PROJECT_ID.length > 0
  );
}

export async function saveDevicePushToken(token: string): Promise<boolean> {
  if (!isFirebaseReady()) {
    console.log('[PushService] Firebase not enabled or not configured, skipping device token save');
    return false;
  }
  try {
    const deviceId = `device_${token.slice(0, 16)}`;
    const documentPath = `${FIRESTORE_BASE}/devices/${deviceId}`;
    const response = await fetch(`${documentPath}?key=${FIREBASE_API_KEY}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        fields: {
          pushToken: { stringValue: token },
          updatedAt: { timestampValue: new Date().toISOString() },
        },
      }),
    });
    return response.ok;
  } catch (error) {
    console.error('[PushService] Failed to save device token:', error);
    return false;
  }
}

export async function saveFamilyMemberPushToken(
  memberId: string,
  name: string,
  pushToken?: string
): Promise<boolean> {
  if (!isFirebaseReady()) {
    console.log('[PushService] Firebase not enabled or not configured, skipping family member token save');
    return false;
  }
  if (!pushToken) return false;
  try {
    const documentPath = `${FIRESTORE_BASE}/familyMembers/${memberId}`;
    const response = await fetch(`${documentPath}?key=${FIREBASE_API_KEY}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        fields: {
          name: { stringValue: name },
          pushToken: { stringValue: pushToken },
          updatedAt: { timestampValue: new Date().toISOString() },
        },
      }),
    });
    return response.ok;
  } catch (error) {
    console.error('[PushService] Failed to save family member token:', error);
    return false;
  }
}

const EXPO_PUSH_TOKEN_REGEX = /^ExponentPushToken\[[\w-]+\]$/;

export function isValidExpoPushToken(token: string): boolean {
  return EXPO_PUSH_TOKEN_REGEX.test(token.trim());
}

export interface SendPushResult {
  success: boolean;
  results?: { ok: boolean; error?: string }[];
  sent?: number;
  failed?: number;
  error?: string;
}

export async function sendRealPushNotification(
  tokens: string[],
  title: string,
  body: string,
  memberIds?: string[]
): Promise<SendPushResult> {
  if (!PUSH_WORKER_ENABLED) {
    console.log('[PushService] Push Worker not enabled, skipping real send');
    return { success: false, error: 'Push Worker not enabled' };
  }
  if (!tokens || tokens.length === 0) {
    return { success: false, error: 'No push tokens provided' };
  }
  try {
    const response = await fetch(PUSH_WORKER_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tokens, title, body, ...(memberIds ? { memberIds } : {}) }),
    });
    const data = await response.json();
    return data as SendPushResult;
  } catch (error) {
    console.error('[PushService] Failed to send real push:', error);
    return { success: false, error: String(error) };
  }
}