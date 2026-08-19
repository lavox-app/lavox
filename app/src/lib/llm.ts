// LLM-vezeték: OpenRouter-hívás + a felhasználó saját API-kulcsának tárolása.
//
// Korábban ez a `polish.ts`-ben lakott, a diktálás-átíró profilok mellett.
// A profilokat (Formális/Informális/Marketing/Visual/Coding/Saját + Prompter)
// kivezettük, mert félkészek voltak; a puszta LLM-hívás viszont kell, mert a
// meeting-összefoglaló (summarize.ts) erre épül.

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
