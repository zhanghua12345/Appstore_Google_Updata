package auth

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tamtom/play-console-cli/internal/config"
)

type fakeRunner struct {
	lookPathErr error
	calls       [][]string
	// responses keyed by first two args joined by space (e.g. "gcloud config").
	responses map[string]runResponse

	// Install/browser/clipboard stubs.
	installErr       error
	installFixesPath bool // when true, LookPath succeeds after InstallGcloud ran
	installed        bool
	interactiveErr   error
	interactiveCalls [][]string
	openedURL        string
	copiedText       string

	// authListSeq, when set, is consumed one entry per `gcloud auth list` call
	// (falls back to the responses map once exhausted). Lets a test simulate
	// "unauthenticated, then authenticated after login".
	authListSeq []runResponse
}

type runResponse struct {
	stdout []byte
	err    error
}

func (f *fakeRunner) LookPath(name string) (string, error) {
	if f.lookPathErr != nil && (!f.installFixesPath || !f.installed) {
		return "", f.lookPathErr
	}
	return "/usr/local/bin/" + name, nil
}

func (f *fakeRunner) Run(ctx context.Context, stdin []byte, name string, args ...string) ([]byte, error) {
	argv := append([]string{name}, args...)
	f.calls = append(f.calls, argv)
	if len(f.authListSeq) > 0 && matchesPrefix(argv, []string{"gcloud", "auth", "list"}) {
		resp := f.authListSeq[0]
		f.authListSeq = f.authListSeq[1:]
		return resp.stdout, resp.err
	}
	for key, resp := range f.responses {
		parts := strings.Fields(key)
		if matchesPrefix(argv, parts) {
			return resp.stdout, resp.err
		}
	}
	return nil, nil
}

func (f *fakeRunner) RunInteractive(ctx context.Context, name string, args ...string) error {
	f.interactiveCalls = append(f.interactiveCalls, append([]string{name}, args...))
	return f.interactiveErr
}

func (f *fakeRunner) InstallGcloud(ctx context.Context) error {
	f.installed = true
	return f.installErr
}

func (f *fakeRunner) OpenBrowser(url string) error {
	f.openedURL = url
	return nil
}

func (f *fakeRunner) Copy(text string) error {
	f.copiedText = text
	return nil
}

// activeAccount is the canned response that marks gcloud as authenticated.
var activeAccount = runResponse{stdout: []byte("dev@example.com\n")}

func matchesPrefix(argv, prefix []string) bool {
	if len(prefix) > len(argv) {
		return false
	}
	for i, p := range prefix {
		if argv[i] != p {
			return false
		}
	}
	return true
}

func writeFakeKey(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(map[string]string{
		"type":         "service_account",
		"client_email": "play-console-cli@my-project.iam.gserviceaccount.com",
	})
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestRunSetupRequiresAuto(t *testing.T) {
	err := RunSetup(context.Background(), SetupOptions{Auto: false}, os.Stdout)
	if err == nil || !strings.Contains(err.Error(), "--auto") {
		t.Fatalf("expected guidance about --auto, got %v", err)
	}
}

func TestRunSetupRequiresGcloudWhenNoInstall(t *testing.T) {
	runner := &fakeRunner{lookPathErr: errors.New("not found")}
	err := RunSetup(context.Background(), SetupOptions{Auto: true, NoInstall: true, Runner: runner}, os.Stdout)
	if err == nil || !strings.Contains(err.Error(), "gcloud") {
		t.Fatalf("expected gcloud error, got %v", err)
	}
	if runner.installed {
		t.Error("should not attempt install when --no-install is set")
	}
}

func TestRunSetupInstallsGcloudWhenMissing(t *testing.T) {
	tmp := t.TempDir()
	keyOut := filepath.Join(tmp, "sa.json")
	runner := &fakeRunner{
		lookPathErr:      errors.New("not found"),
		installFixesPath: true,
		responses: map[string]runResponse{
			"gcloud auth list":                        activeAccount,
			"gcloud config get-value project":         {stdout: []byte("my-proj\n")},
			"gcloud iam service-accounts describe":    {err: errors.New("not found")},
			"gcloud iam service-accounts keys create": {},
		},
	}
	writeFakeKey(t, keyOut)
	opts := SetupOptions{
		Auto:       true,
		Project:    "my-proj",
		Runner:     runner,
		KeyOut:     keyOut,
		NoBrowser:  true,
		HomeDir:    func() (string, error) { return tmp, nil },
		SaveConfig: func(config.Profile, bool) (string, error) { return "", nil },
		Output:     "json",
	}
	if err := RunSetup(context.Background(), opts, os.Stdout); err != nil {
		t.Fatalf("RunSetup: %v", err)
	}
	if !runner.installed {
		t.Error("expected InstallGcloud to be called when gcloud is missing")
	}
}

func TestRunSetupLogsInWhenNotAuthenticated(t *testing.T) {
	tmp := t.TempDir()
	keyOut := filepath.Join(tmp, "sa.json")
	runner := &fakeRunner{
		// Empty first (unauthenticated), active after login runs.
		authListSeq: []runResponse{{}, activeAccount},
		responses: map[string]runResponse{
			"gcloud config get-value project":         {stdout: []byte("my-proj\n")},
			"gcloud iam service-accounts describe":    {err: errors.New("not found")},
			"gcloud iam service-accounts keys create": {},
		},
	}
	writeFakeKey(t, keyOut)
	opts := SetupOptions{
		Auto:       true,
		Project:    "my-proj",
		Runner:     runner,
		KeyOut:     keyOut,
		HomeDir:    func() (string, error) { return tmp, nil },
		SaveConfig: func(config.Profile, bool) (string, error) { return "", nil },
		Output:     "json",
	}
	if err := RunSetup(context.Background(), opts, os.Stdout); err != nil {
		t.Fatalf("RunSetup: %v", err)
	}
	if len(runner.interactiveCalls) != 1 {
		t.Fatalf("expected one interactive login call, got %v", runner.interactiveCalls)
	}
	got := strings.Join(runner.interactiveCalls[0], " ")
	if got != "gcloud auth login" {
		t.Errorf("expected `gcloud auth login`, got %q", got)
	}
}

func TestRunSetupNoBrowserFailsWhenUnauthenticated(t *testing.T) {
	runner := &fakeRunner{
		responses: map[string]runResponse{
			// no "gcloud auth list" response -> empty -> unauthenticated
			"gcloud config get-value project": {stdout: []byte("my-proj\n")},
		},
	}
	err := RunSetup(context.Background(), SetupOptions{
		Auto:      true,
		Project:   "my-proj",
		NoBrowser: true,
		Runner:    runner,
	}, os.Stdout)
	if err == nil || !strings.Contains(err.Error(), "gcloud auth login") {
		t.Fatalf("expected auth login guidance, got %v", err)
	}
	if len(runner.interactiveCalls) != 0 {
		t.Error("should not launch interactive login with --no-browser")
	}
}

func TestRunSetupRequiresProject(t *testing.T) {
	runner := &fakeRunner{
		responses: map[string]runResponse{
			"gcloud auth list":                activeAccount,
			"gcloud config get-value project": {stdout: []byte("(unset)\n")},
		},
	}
	err := RunSetup(context.Background(), SetupOptions{Auto: true, Runner: runner}, os.Stdout)
	if err == nil || !strings.Contains(err.Error(), "project") {
		t.Fatalf("expected project error, got %v", err)
	}
}

func TestRunSetupDryRunPrintsSteps(t *testing.T) {
	runner := &fakeRunner{
		responses: map[string]runResponse{
			"gcloud config get-value project": {stdout: []byte("my-proj\n")},
		},
	}
	tmp := t.TempDir()
	keyOut := filepath.Join(tmp, "sa.json")
	opts := SetupOptions{
		Auto:    true,
		Runner:  runner,
		DryRun:  true,
		KeyOut:  keyOut,
		HomeDir: func() (string, error) { return tmp, nil },
		Output:  "json",
	}
	if err := RunSetup(context.Background(), opts, os.Stdout); err != nil {
		t.Fatalf("RunSetup: %v", err)
	}
	if _, err := os.Stat(keyOut); err == nil {
		t.Error("dry-run should not write key file")
	}
}

func TestRunSetupHappyPathCreatesKeyAndConfig(t *testing.T) {
	tmp := t.TempDir()
	keyOut := filepath.Join(tmp, "sa.json")
	runner := &fakeRunner{
		responses: map[string]runResponse{
			"gcloud auth list":                activeAccount,
			"gcloud config get-value project": {stdout: []byte("my-proj\n")},
			// describe returns error -> triggers create
			"gcloud iam service-accounts describe": {err: errors.New("not found")},
			// keys create "creates" the key (the test writes it)
			"gcloud iam service-accounts keys create": {},
		},
	}
	saved := false
	opts := SetupOptions{
		Auto:    true,
		Project: "my-proj",
		Runner:  runner,
		KeyOut:  keyOut,
		HomeDir: func() (string, error) { return tmp, nil },
		SaveConfig: func(profile config.Profile, setDefault bool) (string, error) {
			saved = true
			return filepath.Join(tmp, "config.json"), nil
		},
		Output: "json",
	}

	// Pre-write the key so that post-key validation passes. In real flow
	// gcloud writes the file. We simulate that by writing it just before the
	// validate step via runner hook — simpler: write ahead of time.
	writeFakeKey(t, keyOut)

	if err := RunSetup(context.Background(), opts, os.Stdout); err != nil {
		t.Fatalf("RunSetup: %v", err)
	}
	if !saved {
		t.Error("expected SaveConfig to be called")
	}
	// gcloud should have been invoked at least 4 times.
	if len(runner.calls) < 4 {
		t.Errorf("expected >=4 gcloud calls, got %d", len(runner.calls))
	}
	// Final step opens Play Console and copies the SA email to the clipboard.
	if runner.openedURL == "" {
		t.Error("expected Play Console to be opened in the browser")
	}
	if !strings.Contains(runner.copiedText, "@my-proj.iam.gserviceaccount.com") {
		t.Errorf("expected SA email copied to clipboard, got %q", runner.copiedText)
	}
}

func TestRunSetupNoBrowserSkipsOpen(t *testing.T) {
	tmp := t.TempDir()
	keyOut := filepath.Join(tmp, "sa.json")
	runner := &fakeRunner{
		responses: map[string]runResponse{
			"gcloud auth list":                        activeAccount,
			"gcloud iam service-accounts describe":    {err: errors.New("not found")},
			"gcloud iam service-accounts keys create": {},
		},
	}
	writeFakeKey(t, keyOut)
	opts := SetupOptions{
		Auto:       true,
		Project:    "my-proj",
		Runner:     runner,
		KeyOut:     keyOut,
		NoBrowser:  true,
		HomeDir:    func() (string, error) { return tmp, nil },
		SaveConfig: func(config.Profile, bool) (string, error) { return "", nil },
		Output:     "json",
	}
	if err := RunSetup(context.Background(), opts, os.Stdout); err != nil {
		t.Fatalf("RunSetup: %v", err)
	}
	if runner.openedURL != "" {
		t.Errorf("expected no browser open with --no-browser, got %q", runner.openedURL)
	}
}

func TestValidateServiceAccountKeyRejectsBadJSON(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bad.json")
	if err := os.WriteFile(path, []byte("not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := validateServiceAccountKey(path); err == nil {
		t.Error("expected error for bad json")
	}
}

func TestValidateServiceAccountKeyRejectsWrongType(t *testing.T) {
	path := filepath.Join(t.TempDir(), "oauth.json")
	if err := os.WriteFile(path, []byte(`{"type":"oauth2","client_email":"x@y"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := validateServiceAccountKey(path); err == nil {
		t.Error("expected error for wrong type")
	}
}

func TestValidateServiceAccountKeyOK(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sa.json")
	writeFakeKey(t, path)
	if err := validateServiceAccountKey(path); err != nil {
		t.Errorf("expected ok, got %v", err)
	}
}

func TestAuthSetupCommandRegistered(t *testing.T) {
	cmd := AuthCommand()
	found := false
	for _, sub := range cmd.Subcommands {
		if sub.Name == "setup" {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected setup subcommand registered on auth")
	}
}

func TestTopLevelSetupCommand(t *testing.T) {
	cmd := SetupCommand()
	if cmd.Name != "setup" {
		t.Errorf("expected top-level command name 'setup', got %q", cmd.Name)
	}
	if !strings.Contains(cmd.ShortUsage, "gplay setup") {
		t.Errorf("expected usage to mention `gplay setup`, got %q", cmd.ShortUsage)
	}
	if cmd.FlagSet.Lookup("auto") == nil {
		t.Error("expected --auto flag on top-level setup command")
	}
}
