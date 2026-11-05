package cmdtest_test

import (
	"errors"
	"flag"
	"strings"
	"testing"
)

func TestChecks_Help(t *testing.T) {
	stdout, stderr, err := runCommand(t, "checks")
	if !errors.Is(err, flag.ErrHelp) {
		t.Fatalf("expected help error, got %v", err)
	}
	combined := stdout + stderr
	for _, want := range []string{"checks", "apps", "reports", "operations", "upload", "analyze", "content", "classify"} {
		if !strings.Contains(combined, want) {
			t.Fatalf("help should mention %q, got: %q", want, combined)
		}
	}
}

func TestChecksAppsList_MissingAccount(t *testing.T) {
	_, stderr, err := runCommand(t, "checks", "apps", "list")
	if !errors.Is(err, flag.ErrHelp) {
		t.Fatalf("expected usage error, got %v", err)
	}
	if !strings.Contains(stderr, "--account is required") {
		t.Fatalf("stderr should mention missing account, got: %q", stderr)
	}
}

func TestChecksUpload_InvalidBinaryType(t *testing.T) {
	_, _, err := runCommand(t, "checks", "upload", "--account", "123", "--app", "456", "--binary", "app.apk", "--binary-type", "BAD")
	if err == nil || !strings.Contains(err.Error(), "--binary-type must be one of") {
		t.Fatalf("expected binary type validation error, got %v", err)
	}
}

func TestChecksContentClassify_MissingText(t *testing.T) {
	_, stderr, err := runCommand(t, "checks", "content", "classify")
	if !errors.Is(err, flag.ErrHelp) {
		t.Fatalf("expected usage error, got %v", err)
	}
	if !strings.Contains(stderr, "--text is required") {
		t.Fatalf("stderr should mention missing text, got: %q", stderr)
	}
}
