package intern

import (
	"context"
	"encoding/json"
	"go.autonomous.ai/os/system/domain"
)

// Unsupported operations return explicit errors before any external effects.
// Empty capability getters report absence, not a simulated successful operation.
func (*Service) QueuePendingEvent(eventType, msg string, images []string, fixedRunID string) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) SendSlashCommandWithRun(msg string, reqID string, runID string) (string, error) {
	return "", domain.ErrNotSupportedByRuntime
}

func (*Service) SendSlashCommandWithImagesAndRun(msg string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return "", domain.ErrNotSupportedByRuntime
}

func (*Service) GetSessionKey() string { return "" }

func (*Service) SetSessionKey(key string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) SetupAgent(data domain.SetupRequest) error { return domain.ErrNotSupportedByRuntime }

func (*Service) SupportedChannels() []string { return nil }

func (*Service) AddChannel(ctx context.Context, data domain.AddChannelRequest) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) RefreshChannelConfig(ctx context.Context, req domain.RefreshChannelRequest) (runtime string, err error) {
	return "", domain.ErrNotSupportedByRuntime
}

func (*Service) HasWhatsappSession(account string) bool { return false }

func (*Service) PairWhatsapp(ctx context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: domain.ErrNotSupportedByRuntime.Error()}
	close(ch)
	return ch
}

func (*Service) ResetAgent() error { return domain.ErrNotSupportedByRuntime }

func (*Service) RestartAgent() error { return domain.ErrNotSupportedByRuntime }

func (*Service) RefreshModelsConfig() error { return domain.ErrNotSupportedByRuntime }

func (*Service) EnsureOnboarding() error { return domain.ErrNotSupportedByRuntime }

func (*Service) SaveSkill(draft domain.SkillDraft) (path string, err error) {
	return "", domain.ErrNotSupportedByRuntime
}

func (*Service) InstallSkillArchive(archivePath, fallbackName string) (dir string, err error) {
	return "", domain.ErrNotSupportedByRuntime
}

func (*Service) InstallSkillMarkdown(content []byte) (dir string, err error) {
	return "", domain.ErrNotSupportedByRuntime
}

func (*Service) ListSkills() ([]domain.InstalledSkill, error) {
	return nil, domain.ErrNotSupportedByRuntime
}

func (*Service) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return nil, domain.ErrNotSupportedByRuntime
}

func (*Service) ExportSkillArchive(name, destDir string) (string, error) {
	return "", domain.ErrNotSupportedByRuntime
}

func (*Service) ReadSkillFile(name, filePath string) (domain.SkillBundleFile, error) {
	return domain.SkillBundleFile{}, domain.ErrNotSupportedByRuntime
}

func (*Service) DeleteSkill(name string) (path string, err error) {
	return "", domain.ErrNotSupportedByRuntime
}

func (*Service) FetchChatHistory(sessionKey string, limit int) (json.RawMessage, error) {
	return nil, domain.ErrNotSupportedByRuntime
}

func (*Service) GetConfigJSON() (json.RawMessage, error) { return nil, domain.ErrNotSupportedByRuntime }

func (*Service) WriteMCPEntry(name string, entry map[string]any) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) RemoveMCPEntry(name string) (bool, error) {
	return false, domain.ErrNotSupportedByRuntime
}

func (*Service) MarkGuardRun(runID string, snapshotPath string) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) ConsumeGuardRun(runID string) (snapshotPath string, ok bool) { return "", false }

func (*Service) MarkBroadcastRun(runID string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) ConsumeBroadcastRun(runID string) bool { return false }

func (*Service) MarkPoseBucketRun(runID string, bucketID string, worstFilenames []string) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) ConsumePoseBucketRun(runID string) (bucketID string, worstFilenames []string, ok bool) {
	return "", nil, false
}

func (*Service) GetTelegramBotToken() string { return "" }

func (*Service) GetTelegramTargets() ([]domain.TelegramTarget, error) {
	return nil, domain.ErrNotSupportedByRuntime
}

func (*Service) Broadcast(msg string, imagePath string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) SendToUser(telegramID string, msg string, imagePath string) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) SendToHALTTS(text string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) Speak(text string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) SendToHALTTSQueue(text string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) StopTTS() error { return domain.ErrNotSupportedByRuntime }

func (*Service) SetVolume(pct int) error { return domain.ErrNotSupportedByRuntime }

func (*Service) StartHALVoice(deepgramKey, llmKey, sttKey, ttsKey, llmBaseURL, sttBaseURL, ttsBaseURL, ttsVoice, ttsInstructions, ttsProvider string) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) WatchIdentity(ctx context.Context) error { return domain.ErrNotSupportedByRuntime }

func (*Service) UpdateIdentityName(name string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) StartSkillWatcher(ctx context.Context) error { return domain.ErrNotSupportedByRuntime }

func (*Service) StartModelSync(ctx context.Context) error { return domain.ErrNotSupportedByRuntime }

func (*Service) UpdatePrimaryModel(modelKey string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) StartPrimaryModelWatch(ctx context.Context) error {
	return domain.ErrNotSupportedByRuntime
}

func (*Service) GetConfiguredChannel() string { return "" }

func (*Service) CompactSession(sessionKey string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) NewSession(sessionKey string) error { return domain.ErrNotSupportedByRuntime }

func (*Service) ShouldRotateSession(totalTokens, turnsSinceRotation int) bool { return false }
