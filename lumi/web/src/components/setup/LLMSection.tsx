import { ConfiguredHint, LockedField, LockedPasswordField, SectionCard } from "./shared";
import type { LlmLoadedState } from "@/hooks/setup/types";

export function LLMSection({
  active, llmLoaded,
  llmApiKey, setLlmApiKey,
  llmUrl, setLlmUrl,
  llmModel, setLlmModel,
}: {
  active: boolean;
  llmLoaded: LlmLoadedState;
  llmApiKey: string; setLlmApiKey: (v: string) => void;
  llmUrl: string; setLlmUrl: (v: string) => void;
  llmModel: string; setLlmModel: (v: string) => void;
}) {
  return (
    <SectionCard id="llm" title="AI Brain" active={active}>
      {llmLoaded.apiKey ? (
        <ConfiguredHint label="API Key" />
      ) : (
        <LockedPasswordField lockedInitially={false} label="API Key" id="llm_api_key" value={llmApiKey} onChange={setLlmApiKey} placeholder="sk-..." />
      )}
      <LockedField lockedInitially={llmLoaded.baseUrl} label="Base URL" id="llm_url" value={llmUrl} onChange={setLlmUrl} placeholder="https://api.openai.com/v1" />
      <LockedField lockedInitially={llmLoaded.model} label="Model" id="llm_model" value={llmModel} onChange={setLlmModel} placeholder="gpt-4o-mini" />
    </SectionCard>
  );
}
