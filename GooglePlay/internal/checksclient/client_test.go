package checksclient

import (
	"testing"

	"github.com/tamtom/play-console-cli/internal/config"
)

func TestResolveAccountPrecedence(t *testing.T) {
	t.Setenv(checksAccountEnvVar, "env-account")
	cfg := &config.Config{ChecksAccount: "config-account"}
	if got := ResolveAccount("flag-account", cfg); got != "flag-account" {
		t.Fatalf("ResolveAccount flag precedence = %q", got)
	}
	if got := ResolveAccount("", cfg); got != "env-account" {
		t.Fatalf("ResolveAccount env precedence = %q", got)
	}
	t.Setenv(checksAccountEnvVar, "")
	if got := ResolveAccount("", cfg); got != "config-account" {
		t.Fatalf("ResolveAccount config fallback = %q", got)
	}
}
