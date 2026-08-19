import { Brain } from "lucide-react";
import { LockedField, SectionCard } from "./shared";
import { SecretUpdateField } from "@/components/SecretUpdateField";
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
    <SectionCard id="llm" title="AI Brain" active={active} icon={<Brain size={17} />}
      description="The LLM that powers your device. Paste the API key and endpoint from your provider.">
      {/* SecretUpdateField handles both empty (Setup) and configured (Settings)
          states inline — a Pencil icon unlocks the input to rotate the key
          without leaving the page. Previously the configured branch rendered
          ConfiguredHint whose "update →" was an <a href="/setting"> that
          navigated the operator OUT of the AI Brain section (bug when opened
          from /setting itself — landed on /setting#general). */}
      <SecretUpdateField
        label="API Key"
        id="llm_api_key"
        configured={llmLoaded.apiKey}
        value={llmApiKey}
        onChange={setLlmApiKey}
        placeholder="sk-..."
      />
      <LockedField lockedInitially={llmLoaded.baseUrl} label="Base URL" id="llm_url" value={llmUrl} onChange={setLlmUrl} placeholder="https://api.openai.com/v1" />
      <LockedField lockedInitially={llmLoaded.model} label="Model" id="llm_model" value={llmModel} onChange={setLlmModel} placeholder="gpt-4o-mini" />
    </SectionCard>
  );
}
