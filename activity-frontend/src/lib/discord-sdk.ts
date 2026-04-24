import { DiscordSDK } from "@discord/embedded-app-sdk";

let sdkInstance: DiscordSDK | null = null;

export interface DiscordAuth {
  sdk: DiscordSDK;
  accessToken: string;
  guildId: string | null;
  user: { id: string; username: string; avatar: string | null };
}

async function fetchClientId(): Promise<string> {
  const resp = await fetch("/api/config");
  if (!resp.ok) throw new Error("Failed to fetch config");
  const data = await resp.json();
  if (!data.client_id) throw new Error("DISCORD_CLIENT_ID not configured on server");
  return data.client_id;
}

export async function initDiscordSDK(): Promise<DiscordAuth> {
  if (sdkInstance) {
    throw new Error("SDK already initialized");
  }

  const clientId = await fetchClientId();

  const sdk = new DiscordSDK(clientId, {
    disableConsoleLogOverride: true,
  });
  sdkInstance = sdk;

  await sdk.ready();

  const { code } = await sdk.commands.authorize({
    client_id: clientId,
    response_type: "code",
    state: "",
    prompt: "none",
    // rpc.activities.write is required by sdk.commands.setActivity. For
    // Embedded Activities this scope is part of the standard SDK scope set
    // and Discord silently approves it in the activity context (no popup).
    scope: ["identify", "guilds", "rpc.activities.write"],
  });

  // Exchange code for token via our backend
  const tokenResp = await fetch("/api/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });

  if (!tokenResp.ok) {
    const err = await tokenResp.json().catch(() => ({}));
    throw new Error(err.detail || "Token exchange failed");
  }

  const { access_token } = await tokenResp.json();

  const auth = await sdk.commands.authenticate({ access_token });

  return {
    sdk,
    accessToken: access_token,
    guildId: sdk.guildId,
    user: {
      id: auth.user.id,
      username: auth.user.username,
      avatar: auth.user.avatar ?? null,
    },
  };
}

export function getSDK(): DiscordSDK | null {
  return sdkInstance;
}
