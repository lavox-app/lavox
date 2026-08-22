// LLM plumbing: OpenRouter call + storage of the user's own API key.
//
// This used to live in `polish.ts`, next to the dictation rewrite profiles.
// The profiles (Formal/Informal/Marketing/Visual/Coding/Custom + Prompter)
// were retired because they were half-finished; the bare LLM call is still
// needed because the meeting summary (summarize.ts) builds on it.

const OPENROUTER_BASE = "https://openrouter.ai/api/v1";

export const STORAGE_KEY_API_KEY = "lavox-openrouter-key";

export function loadApiKey(): string {
  return localStorage.getItem(STORAGE_KEY_API_KEY) ?? "";
}

export function saveApiKey(key: string) {
  localStorage.setItem(STORAGE_KEY_API_KEY, key);
}

export async function llmCall(
  apiKey: string,
  systemPrompt: string,
  userMessage: string,
  jsonMode = false
): Promise<string> {
  const res = await fetch(`${OPENROUTER_BASE}/chat/completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: "anthropic/claude-haiku-4-5",
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user", content: userMessage },
      ],
      ...(jsonMode ? { response_format: { type: "json_object" } } : {}),
    }),
  });

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`OpenRouter ${res.status}: ${body}`);
  }

  const data = await res.json();
  return (data.choices[0].message.content as string).trim();
}
