import { DiscordSDK } from "@discord/embedded-app-sdk";

let sdkInstance: DiscordSDK | null = null;

export interface DiscordAuth {
  sdk: DiscordSDK;
  accessToken: string;
  guildId: string | null;
  channelId: string | null;
  instanceId: string;
  user: { id: string; username: string; avatar: string | null };
}

/** A startup failure with a message fit to show the user. */
export class StartupError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StartupError";
  }
}

const STARTUP_TIMEOUT_MS = 15000;

async function fetchClientId(): Promise<string> {
  const resp = await fetch("/api/config").catch(() => null);
  if (!resp?.ok) throw new StartupError("Couldn't reach the music bot. Try again in a moment.");
  const data = await resp.json();
  if (!data.client_id) throw new StartupError("The music bot isn't set up for Activities yet.");
  return data.client_id;
}

function createSDK(clientId: string): DiscordSDK {
  try {
    return new DiscordSDK(clientId, { disableConsoleLogOverride: true });
  } catch {
    // The constructor throws when the page is not running inside Discord (no frame_id).
    throw new StartupError("Open this Activity from Discord.");
  }
}

async function connect(): Promise<DiscordAuth> {
  const clientId = await fetchClientId();

  const sdk = createSDK(clientId);
  sdkInstance = sdk;

  await sdk.ready();
  // Lets the PiP tile handle taps (play/pause) instead of Discord expanding the Activity.
  // Unsupported on mobile, where the rejection is expected.
  sdk.commands.setConfig({ use_interactive_pip: true }).catch(() => {});

  let code: string;
  try {
    ({ code } = await sdk.commands.authorize({
      client_id: clientId,
      response_type: "code",
      state: "",
      prompt: "none",
      // rpc.activities.write is required by sdk.commands.setActivity. For
      // Embedded Activities this scope is part of the standard SDK scope set
      // and Discord silently approves it in the activity context (no popup).
      scope: ["identify", "guilds", "rpc.activities.write"],
    }));
  } catch {
    throw new StartupError("Allow access to continue.");
  }

  // Exchange code for token via our backend
  const tokenResp = await fetch("/api/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  }).catch(() => null);

  if (!tokenResp?.ok) {
    const detail = await tokenResp?.json().then(d => d?.detail, () => null);
    if (detail) console.warn("Token exchange failed:", detail);
    throw new StartupError("Couldn't sign you in. Try again.");
  }

  const { access_token } = await tokenResp.json();

  const auth = await sdk.commands.authenticate({ access_token });

  return {
    sdk,
    accessToken: access_token,
    guildId: sdk.guildId,
    channelId: sdk.channelId,
    instanceId: sdk.instanceId,
    user: {
      id: auth.user.id,
      username: auth.user.username,
      avatar: auth.user.avatar ?? null,
    },
  };
}

export async function initDiscordSDK(): Promise<DiscordAuth> {
  if (sdkInstance) {
    throw new StartupError("Reload the Activity to try again.");
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new StartupError("Connecting to Discord is taking too long.")),
      STARTUP_TIMEOUT_MS,
    );
  });
  try {
    return await Promise.race([connect(), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** Message to show for a startup failure: plain text, never an RPC payload or a stack trace. */
export function startupErrorMessage(err: unknown): string {
  if (err instanceof StartupError) return err.message;
  return "Couldn't connect to Discord.";
}

export function getSDK(): DiscordSDK | null {
  return sdkInstance;
}

/** Opens a link in the user's browser; plain navigation is blocked inside the Activity frame. */
export function openExternal(url: string) {
  const sdk = getSDK();
  if (sdk) sdk.commands.openExternalLink({ url }).catch(() => {});
  else window.open(url, "_blank", "noopener");
}
